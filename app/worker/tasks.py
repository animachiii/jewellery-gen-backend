"""The staged, resumable job pipeline (docs/business-rules.md R1/R2/R8/R12).

Stages: classify -> resolve -> submit -> poll -> store -> finalize. Each
stage commits its status transition to Redis before doing further work — "no
stage boundary lives in worker memory" (claude.md). Two ARQ entry points feed
into a shared status-dispatching continuation (`_continue_pipeline`) so a job
can be resumed from ANY non-terminal status without re-running earlier
stages — this is what makes a worker restart mid-`generating` safe, and what
makes the `submitting`-crash guard (R2) work regardless of which entry point
resumed the job.

Design notes (deliberate, documented choices — see phases/phase-1-contract.md
Step 6 for the full reasoning):

- The `submit` stage is never retried at the ARQ-job level. A provider.submit
  exception is caught here directly and the job is parked in `needs_review`
  with `ORPHANED_SUBMIT` (R1) — it is never re-raised, so ARQ never retries
  this function for a submit failure.
- A fresh entry point (worker restart) that finds a job already `submitting`
  never calls the provider again — R2's "never resubmit" guard is enforced
  in `_continue_pipeline` before any provider call happens.
- `poll` runs a bounded loop *within* a single stage call, sleeping between
  attempts (capped by `deadline_at`). If the loop exhausts without reaching a
  terminal provider state, the job is simply left in `generating` — the
  sweeper cron (app/worker/sweeper.py, already built) reaps it on deadline
  expiry. This avoids re-enqueuing separate ARQ jobs for polling.
- Poll outcome `failed` is treated conservatively as an immediate `failed` /
  `PROVIDER_ERROR` — resubmitting to "retry the whole job" would violate R1's
  never-auto-retry-a-submit rule under a different name, so it is not done.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

from app.config import settings
from app.core.logging import bind_job, get_logger
from app.core.observability import capture_needs_review
from app.core.state import transition
from app.models.enums import TERMINAL_STATUSES, ErrorCode, JobStatus, TypeSource
from app.models.job import Job
from app.providers.base import GenerationRequest
from app.providers.factory import get_provider
from app.services.classifier import get_classifier
from app.services.dedupe import record_dedupe
from app.services.matrix import MatrixUnavailableError, current_matrix_version, resolve_matrix_row
from app.storage.factory import get_storage_adapter
from app.store import redis_store
from app.store.sheets_store import SheetsClient, safe_update_job_row
from app.worker.retry import DEFAULT_DELAYS, retry_free

log = get_logger(__name__)

# docs/schema.md §2 — same value as app/api/v1/generate.py's / app/worker/settings.py's
# JOB_LOG_TAB. Duplicated deliberately (see generate.py's comment on the same constant)
# rather than imported, to avoid coupling modules that don't otherwise need each other.
JOB_LOG_TAB = "JobLog"

# Poll stage tuning. Module-level constants (not settings) so tests can
# monkeypatch them to near-zero and keep the suite fast; docs/ai-integration.md
# specifies a 5s poll interval in production.
#
# MAX_POLL_ITERATIONS is a defensive ceiling only -- the loop's real bound is
# the `deadline_at` check on each iteration (docs/ai-integration.md: "Poll
# interval: 5s, capped by deadline_at"). At the previous value of 10, the
# effective cap was 10*5s=50s regardless of deadline_at, which silently
# undershot that documented behavior for any provider slower than ~50s
# end-to-end (FakeProvider's ~10s default latency never exposed this).
# 100_000 * 5s (~5.8 days) is high enough that deadline_at is always the
# real limit for any sane JOB_DEADLINE_SECONDS value.
POLL_SLEEP_SECONDS = 5.0
MAX_POLL_ITERATIONS = 100_000

# Free-stage retry backoff (R1: 3 attempts, 2s/8s/30s). Tests monkeypatch this
# to near-zero delays.
RETRY_DELAYS = DEFAULT_DELAYS


async def _transition_and_persist(
    redis: Redis,
    ctx: dict[str, Any],
    job: Job,
    to: JobStatus,
    *,
    error_code: ErrorCode | None = None,
    error_message: str | None = None,
    **fields: Any,
) -> Job:
    """The shared "transition + persist (+ terminal Sheets/dedupe)" wrapper
    every stage funnels through, per claude.md's state-machine convention:
    never assign job.status directly, and always persist + log in the same
    operation."""
    updated = await transition(
        job, to, error_code=error_code, error_message=error_message, **fields
    )

    await redis_store.update_job(
        redis,
        job.job_id,
        status=updated.status,
        updated_at=updated.updated_at,
        completed_at=updated.completed_at,
        error_code=updated.error_code,
        error_message=updated.error_message,
        **fields,
    )

    if updated.status in TERMINAL_STATUSES:
        await record_dedupe(
            redis, updated.content_hash, updated.job_id, updated.status, mock=updated.mock
        )
        row_index = await redis_store.get_row_index(redis, updated.job_id)
        sheets_client_obj = ctx.get("sheets_client")
        sheets_client: SheetsClient | None = (
            sheets_client_obj if sheets_client_obj is not None else None
        )
        if sheets_client is not None and row_index is not None:
            await safe_update_job_row(
                redis, sheets_client, settings.google_sheet_id, JOB_LOG_TAB, updated, row_index
            )

    return updated


async def _do_classify_body(redis: Redis, ctx: dict[str, Any], job: Job) -> Job:
    """Runs the classifier and branches per docs/ai-integration.md's decision
    table. Assumes `job.status == CLASSIFYING` already (the QUEUED ->
    CLASSIFYING transition happens before this is called)."""
    storage = get_storage_adapter()

    async def _fetch_and_classify() -> Any:
        source_bytes, _ = await storage.get(job.source_ref)
        return await get_classifier().classify(source_bytes)

    result = await retry_free(_fetch_and_classify, delays=RETRY_DELAYS)

    candidate_types: list[dict[str, object]] = [
        {"jewelry_type": p.jewelry_type.value, "confidence": p.confidence}
        for p in result.predictions
    ]
    top = result.predictions[0]

    if not result.is_jewelry:
        return await _transition_and_persist(
            redis,
            ctx,
            job,
            JobStatus.FAILED,
            error_code=ErrorCode.NOT_JEWELRY,
            confidence=top.confidence,
            candidate_types=candidate_types,
        )

    if top.confidence >= settings.classifier_confidence_threshold:
        return await _transition_and_persist(
            redis,
            ctx,
            job,
            JobStatus.RESOLVING,
            jewelry_type_final=top.jewelry_type,
            type_source=TypeSource.CLASSIFIED,
            confidence=top.confidence,
            candidate_types=candidate_types,
        )

    return await _transition_and_persist(
        redis,
        ctx,
        job,
        JobStatus.NEEDS_INPUT,
        error_code=ErrorCode.LOW_CONFIDENCE,
        confidence=top.confidence,
        candidate_types=candidate_types,
    )


async def _resolve_and_submit(redis: Redis, ctx: dict[str, Any], job: Job) -> Job:
    """Assumes `job.status == RESOLVING`. Looks up the matrix row, writes the
    snapshot fields once immutably (R8), transitions to SUBMITTING, and
    chains straight into the submit stage."""
    assert job.jewelry_type_final is not None
    jewelry_type_final = job.jewelry_type_final
    sheets_client = ctx.get("sheets_client")

    async def _do_resolve() -> Any:
        return await resolve_matrix_row(
            redis, sheets_client, settings.google_sheet_id, jewelry_type_final, job.service
        )

    try:
        row = await retry_free(_do_resolve, delays=RETRY_DELAYS)
    except MatrixUnavailableError:
        log.warning("matrix.unavailable", job_id=job.job_id)
        return await _transition_and_persist(
            redis, ctx, job, JobStatus.FAILED, error_code=ErrorCode.MATRIX_UNAVAILABLE
        )

    if row is None:
        return await _transition_and_persist(
            redis, ctx, job, JobStatus.FAILED, error_code=ErrorCode.MATRIX_MISS
        )

    matrix_version = await current_matrix_version(redis, sheets_client, settings.google_sheet_id)

    job = await _transition_and_persist(
        redis,
        ctx,
        job,
        JobStatus.SUBMITTING,
        prompt_snapshot=row.prompt,
        negative_prompt_snapshot=row.negative_prompt,
        reference_url_snapshot=row.reference_url,
        provider_params_snapshot=row.params,
        matrix_version=matrix_version,
    )
    return await _submit(redis, ctx, job)


async def _submit(redis: Redis, ctx: dict[str, Any], job: Job) -> Job:
    """Assumes `job.status == SUBMITTING` already persisted. R2: writes
    `submission_token` before calling the provider. R1: never re-raises a
    submit failure for ARQ to retry — parks the job in `needs_review` instead.
    """
    submission_token = uuid4().hex
    await redis_store.update_job(redis, job.job_id, submission_token=submission_token)
    job = replace(job, submission_token=submission_token)

    provider = get_provider(mock=job.mock)
    storage = get_storage_adapter()
    source_bytes, _ = await storage.get(job.source_ref)

    req = GenerationRequest(
        source_image=source_bytes,
        reference_image_url=job.reference_url_snapshot or "",
        prompt=job.prompt_snapshot or "",
        negative_prompt=job.negative_prompt_snapshot,
        params=job.provider_params_snapshot,
        submission_token=submission_token,
    )

    try:
        submission = await provider.submit(req)
    except Exception:
        log.warning("provider.submit.failed", job_id=job.job_id)
        capture_needs_review(job.job_id, reason="provider.submit raised")
        return await _transition_and_persist(
            redis, ctx, job, JobStatus.NEEDS_REVIEW, error_code=ErrorCode.ORPHANED_SUBMIT
        )

    job = await _transition_and_persist(
        redis,
        ctx,
        job,
        JobStatus.GENERATING,
        provider_job_id=submission.provider_job_id,
        provider=provider.name,
    )
    return await _poll(redis, ctx, job)


async def _poll(redis: Redis, ctx: dict[str, Any], job: Job) -> Job:
    """Assumes `job.status == GENERATING`. Bounded poll loop within a single
    stage call, capped by `deadline_at`. Leaves the job in `generating` if the
    loop exhausts without a terminal provider state — the sweeper cron reaps
    it on deadline expiry (R12); this stage does not duplicate that logic.

    A poll call failing (after retry_free's own 3 attempts exhaust) does NOT
    end the loop -- docs/ai-integration.md: "Polling is free — retry it
    freely." It sleeps and tries again next iteration, same as a successful
    "pending"/"running" poll, bounded by the same deadline_at check as
    everything else here. Previously this `break`d out of the whole loop on
    a single retry_free exhaustion, silently abandoning the job in
    `generating` until the sweeper reaped it at deadline_at as
    PROVIDER_TIMEOUT — turning one transient network blip into a permanent
    stall, found via manual testing against the real Higgsfield MCP bridge.
    """
    provider = get_provider(mock=job.mock)
    assert job.provider_job_id is not None

    async def _do_poll() -> Any:
        return await provider.poll(job.provider_job_id)  # type: ignore[arg-type]

    for _ in range(MAX_POLL_ITERATIONS):
        if datetime.now(UTC) >= job.deadline_at:
            break

        try:
            poll_status = await retry_free(_do_poll, delays=RETRY_DELAYS)
        except Exception:
            log.warning("provider.poll.failed", job_id=job.job_id)
        else:
            if poll_status.state == "succeeded":
                job = await _transition_and_persist(redis, ctx, job, JobStatus.STORING)
                return await _store(redis, ctx, job)

            if poll_status.state == "failed":
                return await _transition_and_persist(
                    redis,
                    ctx,
                    job,
                    JobStatus.FAILED,
                    error_code=ErrorCode.PROVIDER_ERROR,
                    attempt_count=job.attempt_count + 1,
                )

        if datetime.now(UTC) >= job.deadline_at:
            break
        await asyncio.sleep(POLL_SLEEP_SECONDS)

    return job


async def _store(redis: Redis, ctx: dict[str, Any], job: Job) -> Job:
    """Assumes `job.status == STORING`. Downloads assets and pushes them
    through the storage adapter, recording `asset_refs` in index order."""
    provider = get_provider(mock=job.mock)
    storage = get_storage_adapter()
    assert job.provider_job_id is not None

    async def _fetch() -> Any:
        return await provider.fetch_assets(job.provider_job_id)  # type: ignore[arg-type]

    try:
        assets = await retry_free(_fetch, delays=RETRY_DELAYS)
        asset_refs: list[str] = []
        for i, asset in enumerate(assets):
            ref = await storage.put(asset.data, filename=f"{job.job_id}_{i}", mime=asset.mime)
            asset_refs.append(ref)
    except Exception:
        log.warning("job.store.failed", job_id=job.job_id)
        return await _transition_and_persist(
            redis, ctx, job, JobStatus.FAILED, error_code=ErrorCode.STORAGE_ERROR
        )

    return await _transition_and_persist(
        redis, ctx, job, JobStatus.SUCCEEDED, asset_refs=asset_refs
    )


async def _continue_pipeline(redis: Redis, ctx: dict[str, Any], job: Job) -> None:
    """Status-dispatching continuation. A job can be resumed from ANY
    non-terminal status without re-running earlier stages — this is what
    makes a worker restart safe at any stage boundary.

    Phase 8 Step 6 (log correlation review): wraps the whole dispatch in
    bind_job() so every log line emitted anywhere in this call tree carries
    job_id automatically -- including ones outside this module's control
    that don't already pass job_id as an explicit kwarg (found via this
    review: app/providers/higgsfield.py's "higgsfield.submit" line and
    app/services/classifier.py's "classifier.gemini.call_*" lines). Every
    call site within this module already passes job_id explicitly too
    (unaffected either way, since _inject_job_id only fills it in when
    absent), so this is additive, not a behavior change for those lines."""
    status = job.status

    if status in TERMINAL_STATUSES:
        return

    with bind_job(job.job_id):
        await _dispatch(redis, ctx, job, status)


async def _dispatch(redis: Redis, ctx: dict[str, Any], job: Job, status: JobStatus) -> None:
    if status == JobStatus.QUEUED:
        if job.jewelry_type_requested is not None:
            # R11: client-supplied type skips classification entirely.
            job = await _transition_and_persist(
                redis,
                ctx,
                job,
                JobStatus.RESOLVING,
                jewelry_type_final=job.jewelry_type_requested,
                type_source=TypeSource.PROVIDED,
            )
            await _resolve_and_submit(redis, ctx, job)
        else:
            job = await _transition_and_persist(redis, ctx, job, JobStatus.CLASSIFYING)
            job = await _do_classify_body(redis, ctx, job)
            if job.status == JobStatus.RESOLVING:
                await _resolve_and_submit(redis, ctx, job)
    elif status == JobStatus.CLASSIFYING:
        job = await _do_classify_body(redis, ctx, job)
        if job.status == JobStatus.RESOLVING:
            await _resolve_and_submit(redis, ctx, job)
    elif status == JobStatus.RESOLVING:
        await _resolve_and_submit(redis, ctx, job)
    elif status == JobStatus.SUBMITTING:
        # A fresh entry point found the job already `submitting` — i.e. a
        # worker died mid-submit. R2: never resubmit. Park it for a human.
        log.warning("worker.job.resumed_in_submitting", job_id=job.job_id)
        capture_needs_review(job.job_id, reason="worker resumed a job already submitting")
        await _transition_and_persist(
            redis, ctx, job, JobStatus.NEEDS_REVIEW, error_code=ErrorCode.ORPHANED_SUBMIT
        )
    elif status == JobStatus.GENERATING:
        await _poll(redis, ctx, job)
    elif status == JobStatus.STORING:
        await _store(redis, ctx, job)


async def run_job_pipeline(ctx: dict[str, Any], job_id: str) -> None:
    """ARQ entry point for a freshly-submitted job (POST /generate)."""
    redis: Redis = ctx["app_redis"]
    job = await redis_store.get_job(redis, job_id)
    if job is None:
        log.warning("worker.job.not_found", job_id=job_id)
        return
    await _continue_pipeline(redis, ctx, job)


async def run_job_pipeline_from_resolve(ctx: dict[str, Any], job_id: str) -> None:
    """ARQ entry point for POST /jobs/{id}/resolve — the job is already
    `resolving` with `jewelry_type_final` set. Classification is NOT re-run
    (R10)."""
    redis: Redis = ctx["app_redis"]
    job = await redis_store.get_job(redis, job_id)
    if job is None:
        log.warning("worker.job.not_found", job_id=job_id)
        return
    await _continue_pipeline(redis, ctx, job)

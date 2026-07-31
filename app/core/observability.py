"""Phase 8 — Sentry initialization (free-tier error tracking + needs_review
alerting, not general log observability; raw structured logs already go to
stdout via app/core/logging.py and are viewable through the deploy
platform's own log viewer at no extra cost).

Two call sites, since the API and the ARQ worker are separate Python
processes: app/main.py's lifespan and app/worker/settings.py's on_startup.
Both call init_sentry() the same way.
"""

import sentry_sdk

from app.config import settings
from app.core.logging import current_job_id, get_logger

log = get_logger(__name__)


def init_sentry() -> None:
    """No-op when SENTRY_DSN is unset — local dev and any environment without
    a configured DSN must behave exactly as if this module didn't exist."""
    if not settings.sentry_dsn:
        log.info("sentry.disabled", reason="SENTRY_DSN not configured")
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.env,
        # Free tier's event quota is for errors, not spans -- this project
        # has no need for distributed tracing/APM.
        traces_sample_rate=0,
        # Never let Sentry's own defaults capture request bodies/headers --
        # same spirit as Hard Rules 8/9 (never log image bytes, API keys,
        # base64 payloads), applied to error reports too.
        send_default_pii=False,
    )
    log.info("sentry.enabled", environment=settings.env)


def capture_needs_review(job_id: str, *, reason: str) -> None:
    """Alerting hook for Step 3: called wherever a job lands in NEEDS_REVIEW
    (a possible orphaned paid charge) -- both the sweeper reaping a stale
    deadline and a live submit failure in app/worker/tasks.py's _submit.

    Deliberately does NOT gate on settings.sentry_dsn (unlike init_sentry())
    -- sentry_sdk.capture_message is already a safe no-op when no client is
    initialized, and gating on the config value instead of the SDK's actual
    state would make this impossible to exercise in a test that initializes
    a real (fake-DSN) Sentry client without also mutating global settings."""
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("job_id", job_id)
        scope.set_tag("alert_type", "needs_review")
        sentry_sdk.capture_message(
            f"Job {job_id} parked in needs_review: {reason}", level="warning"
        )


def bind_sentry_job_scope() -> None:
    """Reuses app/core/logging.py's current_job_id() (backed by the same
    contextvar bind_job() sets) rather than inventing a second job-id-
    threading mechanism. Call this anywhere a Sentry event might be captured
    within a job-scoped context, so the report correlates back to the job log."""
    job_id = current_job_id()
    if job_id is not None:
        sentry_sdk.set_tag("job_id", job_id)

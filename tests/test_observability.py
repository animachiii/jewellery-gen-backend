"""Phase 8 Step 1/2 — Sentry initialization and job-scope correlation.

docs/conventions.md -> Testing: no test may call a real external service.
Sentry's own SDK never makes a network call from init_sentry() itself
(events are queued/flushed by a background transport), but these tests still
never configure a real DSN -- they assert on sentry_sdk's own state
(Hub.current.client) and on mocked capture calls, never on anything reaching
sentry.io.
"""

import sentry_sdk
from pytest import MonkeyPatch

from app.config import settings
from app.core.logging import bind_job, current_job_id
from app.core.observability import bind_sentry_job_scope, capture_needs_review, init_sentry


def test_init_sentry_is_a_noop_without_a_dsn(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sentry_dsn", None)
    was_initialized = sentry_sdk.is_initialized()

    init_sentry()

    # No new client bound -- initialization state is unchanged either way.
    assert sentry_sdk.is_initialized() == was_initialized


def test_init_sentry_configures_client_when_dsn_present(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "sentry_dsn", "https://fake_public_key@fake.ingest.sentry.io/123456"
    )

    init_sentry()
    try:
        assert sentry_sdk.is_initialized()
        client = sentry_sdk.get_client()
        assert client.options["traces_sample_rate"] == 0
        assert client.options["send_default_pii"] is False
    finally:
        sentry_sdk.get_global_scope().set_client(None)


def test_capture_needs_review_is_harmless_when_sentry_is_not_initialized() -> None:
    """No client bound (the default in every test unless a fixture inits one)
    -- sentry_sdk.capture_message is itself a safe no-op in that state, so
    this just confirms calling capture_needs_review never raises."""
    sentry_sdk.get_global_scope().set_client(None)
    capture_needs_review("job-1", reason="test")


def test_capture_needs_review_captures_a_message_with_the_right_shape(
    monkeypatch: MonkeyPatch,
) -> None:
    """Doesn't gate on settings.sentry_dsn -- see capture_needs_review's own
    docstring for why. What matters is the message/level shape it produces,
    verified here via a direct monkeypatch of sentry_sdk.capture_message."""
    captured: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda msg, level=None: captured.append((msg, level)),
    )

    capture_needs_review("job-42", reason="orphaned submit")

    assert len(captured) == 1
    message, level = captured[0]
    assert "job-42" in message
    assert "orphaned submit" in message
    assert level == "warning"


def test_bind_sentry_job_scope_tags_the_current_job_id(monkeypatch: MonkeyPatch) -> None:
    tags: dict[str, str] = {}
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda k, v: tags.__setitem__(k, v))

    with bind_job("job-scope-test"):
        assert current_job_id() == "job-scope-test"
        bind_sentry_job_scope()

    assert tags.get("job_id") == "job-scope-test"


def test_bind_sentry_job_scope_is_a_noop_outside_any_job_context(
    monkeypatch: MonkeyPatch,
) -> None:
    tags: dict[str, str] = {}
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda k, v: tags.__setitem__(k, v))

    bind_sentry_job_scope()

    assert tags == {}

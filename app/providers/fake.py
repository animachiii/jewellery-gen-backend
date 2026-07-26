import io
import time
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from PIL import Image

from app.config import settings
from app.providers.base import (
    GenerationRequest,
    ProviderAsset,
    ProviderStatus,
    ProviderSubmission,
)

FakeFailMode = Literal["submit", "poll", "timeout", "none"]

# Polls needed before a "none" (success) job transitions pending -> running -> succeeded.
_POLLS_TO_RUNNING = 1
_POLLS_TO_SUCCEEDED = 2


@dataclass
class _Submission:
    submission_token: str
    poll_count: int = 0
    started_at: float = field(default_factory=time.monotonic)


class FakeProvider:
    """Test/dev provider. Simulates the real state machine without any network
    calls. Deterministic placeholder assets, configurable latency and failure
    injection via FAKE_FAIL_MODE.
    """

    name = "fake"

    def __init__(
        self,
        latency_seconds: float = 10.0,
        fail_mode: FakeFailMode | None = None,
    ) -> None:
        self.latency_seconds = latency_seconds
        # If fail_mode is None, read settings.fake_fail_mode at call time so
        # tests can monkeypatch settings without reconstructing the provider.
        self._fail_mode_override = fail_mode
        self._submissions: dict[str, _Submission] = {}

    def _fail_mode(self) -> FakeFailMode:
        if self._fail_mode_override is not None:
            return self._fail_mode_override
        return settings.fake_fail_mode

    async def submit(self, req: GenerationRequest) -> ProviderSubmission:
        if self._fail_mode() == "submit":
            raise RuntimeError("FakeProvider: simulated submit failure")

        provider_job_id = uuid4().hex
        self._submissions[provider_job_id] = _Submission(submission_token=req.submission_token)
        return ProviderSubmission(provider_job_id=provider_job_id)

    async def poll(self, provider_job_id: str) -> ProviderStatus:
        mode = self._fail_mode()

        if mode == "poll":
            return ProviderStatus(state="failed", progress=None, error="simulated poll failure")

        if mode == "timeout":
            return ProviderStatus(state="running", progress=0.5, error=None)

        submission = self._submissions.get(provider_job_id)
        if submission is None:
            return ProviderStatus(
                state="failed", progress=None, error="unknown provider_job_id"
            )

        submission.poll_count += 1
        elapsed = time.monotonic() - submission.started_at

        terminal = submission.poll_count > _POLLS_TO_SUCCEEDED or elapsed >= self.latency_seconds
        if terminal:
            return ProviderStatus(state="succeeded", progress=1.0, error=None)

        if submission.poll_count > _POLLS_TO_RUNNING:
            return ProviderStatus(state="running", progress=0.5, error=None)

        return ProviderStatus(state="pending", progress=0.0, error=None)

    async def fetch_assets(self, provider_job_id: str) -> list[ProviderAsset]:
        # Deterministic solid-color placeholder image (same every call).
        img = Image.new("RGB", (64, 64), color=(200, 180, 140))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return [ProviderAsset(data=buf.getvalue(), mime="image/png")]

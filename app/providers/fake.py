import base64
import io
import json
import time
from typing import Literal

from PIL import Image

from app.config import settings
from app.providers.base import (
    GenerationRequest,
    ProviderAsset,
    ProviderStatus,
    ProviderSubmission,
)

FakeFailMode = Literal["submit", "poll", "timeout", "none"]

# Fraction of latency_seconds elapsed before a "none" (success) job reports
# "running" instead of "pending".
_RUNNING_FRACTION = 0.3


class FakeProvider:
    """Test/dev provider. Simulates the real state machine without any network
    calls. Deterministic placeholder assets, configurable latency and failure
    injection via FAKE_FAIL_MODE.

    Stateless by design: `get_provider()` (app/providers/factory.py) is called
    fresh in each pipeline stage (_submit, _poll, _store), so a new FakeProvider
    instance is constructed per call. All information poll()/fetch_assets()
    need is therefore encoded *in* `provider_job_id` itself (a base64 JSON
    blob carrying submit-time timestamps), rather than kept in instance
    memory — otherwise poll() on a fresh instance would never find the
    submission and always report "unknown provider_job_id". time.monotonic()
    is a process-wide clock, so timestamps encoded by one instance remain
    valid when decoded by another instance in the same process.
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

    def _fail_mode(self) -> FakeFailMode:
        if self._fail_mode_override is not None:
            return self._fail_mode_override
        return settings.fake_fail_mode

    async def submit(self, req: GenerationRequest) -> ProviderSubmission:
        if self._fail_mode() == "submit":
            raise RuntimeError("FakeProvider: simulated submit failure")

        now = time.monotonic()
        payload = {
            "token": req.submission_token,
            "running_at": now + self.latency_seconds * _RUNNING_FRACTION,
            "terminal_at": now + self.latency_seconds,
        }
        provider_job_id = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return ProviderSubmission(provider_job_id=provider_job_id)

    async def poll(self, provider_job_id: str) -> ProviderStatus:
        mode = self._fail_mode()

        if mode == "poll":
            return ProviderStatus(state="failed", progress=None, error="simulated poll failure")

        if mode == "timeout":
            return ProviderStatus(state="running", progress=0.5, error=None)

        try:
            payload = json.loads(base64.urlsafe_b64decode(provider_job_id.encode()))
            running_at = float(payload["running_at"])
            terminal_at = float(payload["terminal_at"])
        except Exception:
            return ProviderStatus(state="failed", progress=None, error="unknown provider_job_id")

        now = time.monotonic()
        if now >= terminal_at:
            return ProviderStatus(state="succeeded", progress=1.0, error=None)
        if now >= running_at:
            return ProviderStatus(state="running", progress=0.5, error=None)
        return ProviderStatus(state="pending", progress=0.0, error=None)

    async def fetch_assets(self, provider_job_id: str) -> list[ProviderAsset]:
        # Deterministic solid-color placeholder image (same every call).
        img = Image.new("RGB", (64, 64), color=(200, 180, 140))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return [ProviderAsset(data=buf.getvalue(), mime="image/png")]

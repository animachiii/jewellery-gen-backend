"""Higgsfield-backed GenerationProvider (docs/ai-integration.md §2).

There is no official Higgsfield SDK pinned in this project and this build
environment has no network access to confirm Higgsfield's real wire format.
Every endpoint path, header name, and JSON field name below is a documented
*placeholder* built from the contract in docs/ai-integration.md §2 plus
common REST conventions -- see phases/phase-4-provider-integration.md's
"Manual Verification" section for the full list of what must be confirmed
against Higgsfield's real API docs before this is used against production
traffic. The HTTP layer is isolated behind HiggsfieldClient specifically so
that confirming/correcting these details later is a small, contained change.

R1 (docs/business-rules.md): submit is never retried at this layer or any
layer below the worker. R2's submission_token-before-provider-call ordering
is enforced by the caller (app/worker/tasks.py's `_submit`), not here --
this module only threads the token through to the provider as an
idempotency hint.
"""

from typing import Literal, Protocol

import httpx

from app.config import settings
from app.core.logging import get_logger
from app.providers.base import (
    GenerationRequest,
    ProviderAsset,
    ProviderStatus,
    ProviderSubmission,
)

log = get_logger(__name__)

SUBMIT_TIMEOUT = httpx.Timeout(30.0)
POLL_TIMEOUT = httpx.Timeout(15.0)
FETCH_TIMEOUT = httpx.Timeout(15.0)


class HiggsfieldSubmitRejectedError(Exception):
    """Raised when Higgsfield returns a definite 4xx on submit -- i.e. the
    provider is confirmed to have rejected the job outright (no charge).
    Distinguishable from a timeout/connection error/5xx (ambiguous outcome),
    which raises HiggsfieldRequestError instead.

    docs/schema.md's ErrorCode table has a dedicated PROVIDER_SUBMIT_FAILED
    code for exactly this case, but app/worker/tasks.py's `_submit` today
    catches all submit exceptions uniformly and maps them to
    ORPHANED_SUBMIT/needs_review (the conservative, currently-correct
    behaviour per R1). This exception type exists so a future phase can
    have `_submit` discriminate the two without another provider-layer
    change -- not wired into the worker in this phase. See
    phases/phase-4-provider-integration.md Step 2 / Self-Audit item 4.
    """

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        super().__init__(f"Higgsfield rejected submit with status {status}")


class HiggsfieldRequestError(Exception):
    """Any other Higgsfield HTTP failure: timeout, connection error, 5xx, or
    a response missing an expected field. Ambiguous outcome -- the caller
    must not assume the job was or wasn't accepted."""


def _map_status(raw: str) -> Literal["pending", "running", "succeeded", "failed"]:
    """Isolated so the (unconfirmed) real status vocabulary can be corrected
    in one place. PLACEHOLDER mapping -- confirm against real docs."""
    normalized = raw.strip().lower()
    mapping: dict[str, Literal["pending", "running", "succeeded", "failed"]] = {
        "pending": "pending",
        "queued": "pending",
        "running": "running",
        "processing": "running",
        "in_progress": "running",
        "succeeded": "succeeded",
        "completed": "succeeded",
        "success": "succeeded",
        "failed": "failed",
        "error": "failed",
    }
    if normalized not in mapping:
        raise HiggsfieldRequestError(f"Unrecognised Higgsfield status: {raw!r}")
    return mapping[normalized]


class HiggsfieldTransport(Protocol):
    """The minimal httpx surface HiggsfieldClient needs, injectable for
    tests via httpx.AsyncClient(transport=httpx.MockTransport(...))."""

    async def post(self, url: str, **kwargs: object) -> httpx.Response: ...

    async def get(self, url: str, **kwargs: object) -> httpx.Response: ...


class HiggsfieldClient:
    """Thin wrapper around the actual HTTP calls, constructor-injectable so
    HiggsfieldProvider is testable without live network access -- mirrors
    app/storage/supabase.py's HttpxSupabaseStorageClient pattern."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # PLACEHOLDER auth convention -- confirm against real docs.
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._transport = transport

    def _client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=timeout)

    async def submit(self, req: GenerationRequest) -> str:
        # PLACEHOLDER endpoint/method/body shape -- confirm against real docs.
        url = f"{self._base_url}/v1/generate"
        form: dict[str, object] = {
            "reference_image_url": req.reference_image_url,
            "prompt": req.prompt,
        }
        if req.negative_prompt is not None:
            form["negative_prompt"] = req.negative_prompt
        if req.params:
            for key, value in req.params.items():
                form[f"params[{key}]"] = value

        headers = {**self._headers, "Idempotency-Key": req.submission_token}
        files = {"source_image": ("source.bin", req.source_image, "application/octet-stream")}

        try:
            async with self._client(SUBMIT_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, data=form, files=files)
        except httpx.HTTPError as exc:
            raise HiggsfieldRequestError("Higgsfield submit request failed") from exc

        if 400 <= resp.status_code < 500:
            raise HiggsfieldSubmitRejectedError(resp.status_code, resp.text)
        if resp.status_code >= 500:
            raise HiggsfieldRequestError(f"Higgsfield submit returned {resp.status_code}")

        try:
            body = resp.json()
        except Exception as exc:
            raise HiggsfieldRequestError("Higgsfield submit response was not valid JSON") from exc

        provider_job_id = body.get("id") or body.get("job_id")
        if not provider_job_id:
            raise HiggsfieldRequestError(
                "Higgsfield submit response missing an 'id'/'job_id' field"
            )
        return str(provider_job_id)

    async def poll(self, provider_job_id: str) -> ProviderStatus:
        # PLACEHOLDER endpoint -- confirm against real docs.
        url = f"{self._base_url}/v1/generate/{provider_job_id}"
        try:
            async with self._client(POLL_TIMEOUT) as client:
                resp = await client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise HiggsfieldRequestError("Higgsfield poll request failed") from exc

        if resp.status_code >= 400:
            raise HiggsfieldRequestError(f"Higgsfield poll returned {resp.status_code}")

        try:
            body = resp.json()
        except Exception as exc:
            raise HiggsfieldRequestError("Higgsfield poll response was not valid JSON") from exc

        raw_status = body.get("status")
        if not raw_status:
            raise HiggsfieldRequestError("Higgsfield poll response missing a 'status' field")

        return ProviderStatus(
            state=_map_status(str(raw_status)),
            progress=body.get("progress"),
            error=body.get("error"),
        )

    async def fetch_asset_urls(self, provider_job_id: str) -> list[str]:
        # PLACEHOLDER endpoint -- confirm against real docs. Assumes a
        # separate fetch step rather than URLs embedded in the poll
        # response; simplify if that assumption is wrong.
        url = f"{self._base_url}/v1/generate/{provider_job_id}/assets"
        try:
            async with self._client(FETCH_TIMEOUT) as client:
                resp = await client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise HiggsfieldRequestError("Higgsfield asset-list request failed") from exc

        if resp.status_code >= 400:
            raise HiggsfieldRequestError(f"Higgsfield asset-list returned {resp.status_code}")

        try:
            body = resp.json()
        except Exception as exc:
            raise HiggsfieldRequestError(
                "Higgsfield asset-list response was not valid JSON"
            ) from exc

        urls = body.get("assets") or body.get("urls")
        if not urls:
            raise HiggsfieldRequestError("Higgsfield asset-list response missing assets/urls")
        return [str(u) for u in urls]

    async def download_asset(self, url: str) -> ProviderAsset:
        try:
            async with self._client(FETCH_TIMEOUT) as client:
                resp = await client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise HiggsfieldRequestError("Higgsfield asset download failed") from exc

        if resp.status_code >= 400:
            raise HiggsfieldRequestError(f"Higgsfield asset download returned {resp.status_code}")

        mime = resp.headers.get("content-type", "image/png")
        return ProviderAsset(data=resp.content, mime=mime)


class HiggsfieldProvider:
    """Real GenerationProvider implementation. See module docstring for the
    manual-verification caveats around every wire detail below."""

    name = "higgsfield"

    def __init__(self, client: HiggsfieldClient | None = None) -> None:
        self._client = client or HiggsfieldClient(
            base_url=settings.higgsfield_base_url,
            api_key=settings.higgsfield_api_key or "",
        )

    async def submit(self, req: GenerationRequest) -> ProviderSubmission:
        # No image bytes, base64, or API key logged -- docs/ai-integration.md §4.
        log.info("higgsfield.submit", submission_token=req.submission_token)
        provider_job_id = await self._client.submit(req)
        return ProviderSubmission(provider_job_id=provider_job_id)

    async def poll(self, provider_job_id: str) -> ProviderStatus:
        return await self._client.poll(provider_job_id)

    async def fetch_assets(self, provider_job_id: str) -> list[ProviderAsset]:
        urls = await self._client.fetch_asset_urls(provider_job_id)
        return [await self._client.download_asset(u) for u in urls]

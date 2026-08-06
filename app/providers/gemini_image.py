"""Gemini-backed GenerationProvider (docs/ai-integration.md §2 candidate).

Gemini image generation is a single synchronous call — unlike Higgsfield
there is no submit/poll/fetch lifecycle on the provider's side. To fit the
three-stage `GenerationProvider` protocol (`app/worker/tasks.py`'s `_submit`,
`_poll`, `_store` each construct a fresh provider instance via
`get_provider()` — see `app/providers/fake.py`'s module docstring for why),
the entire generation happens inside `submit()`, and the resulting image
bytes are carried forward base64-encoded inside `provider_job_id` itself —
the same stateless-encoding trick `FakeProvider` uses, just carrying real
image bytes instead of timestamps. `poll()` and `fetch_assets()` then just
decode what `submit()` already produced; neither makes a network call.

This means `provider_job_id` is unusually large for this provider (an
encoded image, not an opaque handle) and is not a real Gemini-side job
identifier — there is no Gemini-side job to look up. Redis is fine with
this size at expected volumes; revisit if generated images grow large
enough to strain the 48h `job:{job_id}` hash (docs/schema.md §3).

SDK note (introspected against the installed `google-genai==0.3.0`, mirrors
`app/services/classifier.py`'s introspection style):

- Image generation uses `GenerateContentConfig(response_modalities=\
  ["TEXT", "IMAGE"])` — `response_modalities` takes plain string literals,
  not an enum with members.
- Image input (both the source photo and the reference image) goes in as
  `types.Part.from_bytes(data=..., mime_type=...)` inside `contents`, same
  as the classifier.
- Output images come back as `response.candidates[0].content.parts[i].\
  inline_data.data` (bytes) / `.mime_type` — there is no dedicated "image"
  accessor on the response, unlike `.text` for pure-text output.
"""

import base64
import json
import re
import time
from typing import Any

import httpx
from google import genai
from google.genai import types
from google.genai._api_client import HttpOptions

from app.config import settings
from app.core.logging import get_logger
from app.providers.base import (
    GenerationRequest,
    ProviderAsset,
    ProviderStatus,
    ProviderSubmission,
)

log = get_logger(__name__)

# docs/ai-integration.md §4 doesn't set a generation-specific timeout yet;
# image generation is slower than classification's 15s, so this provider
# uses its own, longer budget rather than reusing _CLASSIFIER_TIMEOUT_MS.
_GENERATION_TIMEOUT_MS = 60_000
_REFERENCE_FETCH_TIMEOUT = httpx.Timeout(15.0)


class GeminiImageRequestError(Exception):
    """Gemini image generation call failed, returned no image part, or the
    reference image could not be fetched. Ambiguous-outcome equivalent to
    HiggsfieldRequestError — the worker's R1 no-auto-retry-on-submit rule
    applies identically regardless of which provider raises."""


# Matrix rows embed a Google Drive *share* link
# ("https://drive.google.com/file/d/{id}/view?usp=sharing") -- fetching that
# URL directly returns Google's HTML viewer page, not the image bytes.
# Higgsfield never hit this because it receives reference_image_url as a
# string and fetches it server-side itself; this provider is the first
# in-process code to actually download it, so the share-link -> direct-
# download rewrite has to happen here.
_DRIVE_SHARE_RE = re.compile(r"drive\.google\.com/file/d/([^/]+)")


def _to_direct_download_url(url: str) -> str:
    match = _DRIVE_SHARE_RE.search(url)
    if not match:
        return url
    file_id = match.group(1)
    return f"https://drive.google.com/uc?export=download&id={file_id}"


async def _fetch_reference_image(url: str) -> tuple[bytes, str]:
    direct_url = _to_direct_download_url(url)
    try:
        async with httpx.AsyncClient(
            timeout=_REFERENCE_FETCH_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(direct_url)
    except httpx.HTTPError as exc:
        raise GeminiImageRequestError("Failed to fetch reference image") from exc
    if resp.status_code >= 400:
        raise GeminiImageRequestError(f"Reference image fetch returned {resp.status_code}")
    mime = resp.headers.get("content-type", "image/jpeg")
    if not mime.startswith("image/"):
        # Drive serves an HTML "can't scan this file for viruses" interstitial
        # instead of the file itself above a certain size, even on the direct
        # -download URL. A non-image content-type here means the reference
        # image didn't actually come through -- fail loudly rather than hand
        # Gemini an HTML page as an "image" part.
        raise GeminiImageRequestError(
            f"Reference image fetch returned non-image content-type {mime!r}"
        )
    return resp.content, mime


class GeminiImageProvider:
    """Real GenerationProvider implementation backed by Gemini image
    generation. See module docstring for the submit-does-everything /
    encoded-provider_job_id design."""

    name = "gemini_image"

    def __init__(self, client: Any | None = None) -> None:
        self._client = client or genai.Client(
            api_key=settings.gemini_api_key,
            http_options=HttpOptions(timeout=_GENERATION_TIMEOUT_MS),
        )

    async def submit(self, req: GenerationRequest) -> ProviderSubmission:
        # No image bytes, base64, or API key logged -- docs/ai-integration.md §4.
        log.info("gemini_image.submit", submission_token=req.submission_token)
        start = time.monotonic()

        reference_bytes, reference_mime = await _fetch_reference_image(
            req.reference_image_url
        )

        model = settings.gemini_image_model
        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=[
                    req.prompt,
                    types.Part.from_bytes(data=req.source_image, mime_type="image/jpeg"),
                    types.Part.from_bytes(data=reference_bytes, mime_type=reference_mime),
                ],
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                ),
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            # Exception text/type only -- no image bytes, base64, or API key
            # ever land in this string (docs/ai-integration.md §4).
            log.warning(
                "gemini_image.call_failed",
                model=model,
                latency_ms=latency_ms,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise GeminiImageRequestError("Gemini image generation call failed") from exc

        assets = self._extract_assets(response)
        if not assets:
            latency_ms = int((time.monotonic() - start) * 1000)
            log.warning("gemini_image.no_image_returned", model=model, latency_ms=latency_ms)
            raise GeminiImageRequestError("Gemini response contained no image part")

        latency_ms = int((time.monotonic() - start) * 1000)
        log.info(
            "gemini_image.call_completed",
            model=model,
            latency_ms=latency_ms,
            asset_count=len(assets),
        )

        payload = {
            "assets": [
                {"data": base64.b64encode(a.data).decode(), "mime": a.mime} for a in assets
            ]
        }
        provider_job_id = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return ProviderSubmission(provider_job_id=provider_job_id)

    async def poll(self, provider_job_id: str) -> ProviderStatus:
        # Generation already completed inside submit() -- nothing to poll.
        # A decode failure here means a corrupted/foreign provider_job_id,
        # which is a failed job, not a retryable one.
        try:
            self._decode(provider_job_id)
        except Exception:
            return ProviderStatus(state="failed", progress=None, error="unknown provider_job_id")
        return ProviderStatus(state="succeeded", progress=1.0, error=None)

    async def fetch_assets(self, provider_job_id: str) -> list[ProviderAsset]:
        return self._decode(provider_job_id)

    @staticmethod
    def _extract_assets(response: Any) -> list[ProviderAsset]:
        assets: list[ProviderAsset] = []
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline_data = getattr(part, "inline_data", None)
                if inline_data is not None and inline_data.data:
                    assets.append(
                        ProviderAsset(
                            data=inline_data.data,
                            mime=inline_data.mime_type or "image/png",
                        )
                    )
        return assets

    @staticmethod
    def _decode(provider_job_id: str) -> list[ProviderAsset]:
        payload = json.loads(base64.urlsafe_b64decode(provider_job_id.encode()))
        return [
            ProviderAsset(data=base64.b64decode(a["data"]), mime=a["mime"])
            for a in payload["assets"]
        ]

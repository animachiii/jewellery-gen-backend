"""GenerationProvider backed by the local Higgsfield MCP bridge sidecar.

TESTING/SHOWCASE ONLY -- not the intended production path. The real
production provider is app/providers/higgsfield.py, which targets
Higgsfield's direct REST API with a server-side HIGGSFIELD_API_KEY per
docs/ai-integration.md §2. This adapter exists because that API key isn't
available yet, and talks instead to scripts/higgsfield_mcp_prototype/bridge_server.py
-- a small sidecar process that speaks real MCP (OAuth-authenticated,
billed against a personal Higgsfield account's credits) and exposes a
plain REST API this module calls over httpx. See that script's module
docstring and phases/phase-4-provider-integration.md for the full reasoning
on why MCP itself is never spoken from inside this app/worker process.

Delete this file and its factory branch once a real HIGGSFIELD_API_KEY
makes app/providers/higgsfield.py usable -- this is meant to be a two-file
removal, same as any other adapter swap in this codebase.

Known limitations (bridge sidecar, not this module's fault):
- reference_image_url from the matrix is not forwarded -- only the client's
  own source_image is sent as the model's reference (the only path proven
  to work in manual testing). negative_prompt is passed through best-effort
  against an unconfirmed field name.
- Observed latency for one image was ~15 minutes against this project's
  900s JOB_DEADLINE_SECONDS -- expect PROVIDER_TIMEOUT/needs_review more
  often than with a real metered API. Fine for manual testing; do not point
  real client traffic at this.
"""

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


class HiggsfieldMcpBridgeError(Exception):
    """Raised for any bridge-sidecar HTTP failure: sidecar not running,
    MCP call failed inside the sidecar, or an unparseable response."""


class HiggsfieldMcpBridgeProvider:
    """Real GenerationProvider implementation, delegating to the local MCP
    bridge sidecar over plain httpx -- no `mcp` SDK dependency in this
    process. See module docstring for scope and limitations."""

    name = "higgsfield_mcp_bridge"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.higgsfield_mcp_bridge_url).rstrip("/")

    async def submit(self, req: GenerationRequest) -> ProviderSubmission:
        log.info("higgsfield_mcp_bridge.submit", submission_token=req.submission_token)
        files = {"image": ("source.jpg", req.source_image, "image/jpeg")}
        data: dict[str, str] = {"prompt": req.prompt}
        if req.negative_prompt:
            data["negative_prompt"] = req.negative_prompt

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(f"{self._base_url}/submit", data=data, files=files)
        except httpx.HTTPError as exc:
            raise HiggsfieldMcpBridgeError("Bridge submit request failed") from exc

        if resp.status_code >= 400:
            raise HiggsfieldMcpBridgeError(
                f"Bridge submit returned {resp.status_code}: {resp.text}"
            )

        body = resp.json()
        return ProviderSubmission(provider_job_id=body["provider_job_id"])

    async def poll(self, provider_job_id: str) -> ProviderStatus:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(f"{self._base_url}/status/{provider_job_id}")
        except httpx.HTTPError as exc:
            raise HiggsfieldMcpBridgeError("Bridge status request failed") from exc

        if resp.status_code >= 400:
            raise HiggsfieldMcpBridgeError(
                f"Bridge status returned {resp.status_code}: {resp.text}"
            )

        body = resp.json()
        return ProviderStatus(
            state=body["state"], progress=body.get("progress"), error=body.get("error")
        )

    async def fetch_assets(self, provider_job_id: str) -> list[ProviderAsset]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(f"{self._base_url}/assets/{provider_job_id}")
        except httpx.HTTPError as exc:
            raise HiggsfieldMcpBridgeError("Bridge assets request failed") from exc

        if resp.status_code >= 400:
            raise HiggsfieldMcpBridgeError(
                f"Bridge assets returned {resp.status_code}: {resp.text}"
            )

        urls = resp.json()["urls"]
        assets: list[ProviderAsset] = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            for url in urls:
                asset_resp = await client.get(url)
                asset_resp.raise_for_status()
                mime = asset_resp.headers.get("content-type", "image/png")
                assets.append(ProviderAsset(data=asset_resp.content, mime=mime))
        return assets

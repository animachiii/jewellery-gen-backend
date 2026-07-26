from app.providers.base import (
    GenerationRequest,
    ProviderAsset,
    ProviderStatus,
    ProviderSubmission,
)

_NOT_IMPLEMENTED = "Higgsfield integration lands in Phase 4"


class HiggsfieldProvider:
    """Stub. Real integration lands in Phase 4."""

    name = "higgsfield"

    async def submit(self, req: GenerationRequest) -> ProviderSubmission:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def poll(self, provider_job_id: str) -> ProviderStatus:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def fetch_assets(self, provider_job_id: str) -> list[ProviderAsset]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

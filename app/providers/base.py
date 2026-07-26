from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass
class GenerationRequest:
    source_image: bytes
    reference_image_url: str
    prompt: str
    negative_prompt: str | None
    params: dict[str, object] | None
    submission_token: str


@dataclass
class ProviderSubmission:
    provider_job_id: str


@dataclass
class ProviderStatus:
    state: Literal["pending", "running", "succeeded", "failed"]
    progress: float | None
    error: str | None


@dataclass
class ProviderAsset:
    data: bytes
    mime: str


class GenerationProvider(Protocol):
    name: str

    async def submit(self, req: GenerationRequest) -> ProviderSubmission: ...

    async def poll(self, provider_job_id: str) -> ProviderStatus: ...

    async def fetch_assets(self, provider_job_id: str) -> list[ProviderAsset]: ...

from app.config import settings
from app.providers.base import GenerationProvider
from app.providers.fake import FakeProvider
from app.providers.higgsfield import HiggsfieldProvider


def get_provider(mock: bool = False) -> GenerationProvider:
    """Resolve the configured GenerationProvider.

    `mock=True` always resolves to FakeProvider regardless of PROVIDER (R6) —
    a mock job must never be able to reach the real provider.
    """
    if mock or settings.provider == "fake":
        return FakeProvider()
    return HiggsfieldProvider()

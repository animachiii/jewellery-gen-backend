from app.config import settings
from app.providers.base import GenerationProvider
from app.providers.fake import FakeProvider
from app.providers.higgsfield import HiggsfieldProvider
from app.providers.higgsfield_mcp_bridge import HiggsfieldMcpBridgeProvider


def get_provider(mock: bool = False) -> GenerationProvider:
    """Resolve the configured GenerationProvider.

    `mock=True` always resolves to FakeProvider regardless of PROVIDER (R6) —
    a mock job must never be able to reach the real provider.

    `higgsfield_mcp_bridge` is a testing/showcase-only path (real Higgsfield
    generations via a local MCP bridge sidecar, no HIGGSFIELD_API_KEY
    required) -- see app/providers/higgsfield_mcp_bridge.py. Not used unless
    explicitly selected via PROVIDER.
    """
    if mock or settings.provider == "fake":
        return FakeProvider()
    if settings.provider == "higgsfield_mcp_bridge":
        return HiggsfieldMcpBridgeProvider()
    return HiggsfieldProvider()

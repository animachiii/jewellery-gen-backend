from typing import Protocol


class StorageAdapter(Protocol):
    """Async storage abstraction. No code outside app/storage/ may import a
    concrete implementation — resolve via get_storage_adapter()."""

    async def put(self, data: bytes, filename: str, mime: str) -> str:
        """Persist bytes and return an opaque storage_ref (no path separators,
        no filename embedded)."""
        ...

    async def get(self, ref: str) -> tuple[bytes, str]:
        """Return (data, mime) for a previously stored ref."""
        ...

    async def exists(self, ref: str) -> bool:
        """Cheap existence check for a ref."""
        ...

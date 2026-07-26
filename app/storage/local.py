import asyncio
import os
from uuid import uuid4

from app.config import settings


class StorageRefNotFoundError(Exception):
    """Raised when a storage_ref does not correspond to any stored object.

    Message intentionally omits any filesystem path.
    """

    def __init__(self, ref: str) -> None:
        super().__init__(f"storage ref not found: {ref}")
        self.ref = ref


class LocalStorage:
    """Filesystem-backed StorageAdapter. Writes to `settings.local_storage_dir`.

    The returned storage_ref is an opaque uuid4 hex string containing no path
    separators and no filename — the mapping to actual files lives entirely
    inside this class.
    """

    def __init__(self, base_dir: str | None = None) -> None:
        self._base_dir = base_dir if base_dir is not None else settings.local_storage_dir

    def _data_path(self, ref: str) -> str:
        return os.path.join(self._base_dir, ref)

    def _mime_path(self, ref: str) -> str:
        return os.path.join(self._base_dir, f"{ref}.mime")

    async def put(self, data: bytes, filename: str, mime: str) -> str:
        ref = uuid4().hex

        def _write() -> None:
            os.makedirs(self._base_dir, exist_ok=True)
            with open(self._data_path(ref), "wb") as f:
                f.write(data)
            with open(self._mime_path(ref), "w", encoding="utf-8") as f:
                f.write(mime)

        await asyncio.to_thread(_write)
        return ref

    async def get(self, ref: str) -> tuple[bytes, str]:
        def _read() -> tuple[bytes, str]:
            data_path = self._data_path(ref)
            mime_path = self._mime_path(ref)
            if not os.path.isfile(data_path) or not os.path.isfile(mime_path):
                raise StorageRefNotFoundError(ref)
            with open(data_path, "rb") as f:
                data = f.read()
            with open(mime_path, encoding="utf-8") as f:
                mime = f.read()
            return data, mime

        return await asyncio.to_thread(_read)

    async def exists(self, ref: str) -> bool:
        def _check() -> bool:
            return os.path.isfile(self._data_path(ref)) and os.path.isfile(self._mime_path(ref))

        return await asyncio.to_thread(_check)


def get_storage_adapter() -> "LocalStorage":
    # Phase 1 only builds LocalStorage. Phase 2 will branch on a new setting
    # (e.g. STORAGE_BACKEND) to add Drive; not adding that env var now since
    # it isn't in docs/schema.md yet and there is nothing to branch to.
    return LocalStorage()

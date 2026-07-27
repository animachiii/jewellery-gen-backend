"""Supabase Storage-backed StorageAdapter (docs/conventions.md -> Adapters).

Chosen over Google Drive (docs/business-rules.md's "Google Drive quota or
sharing friction in practice" Revisit Trigger, fired in practice: a service
account has zero storage quota of its own on a personal (non-Workspace)
Google Drive, blocking uploads outright). Supabase's Storage REST API is
S3-compatible and designed for service-role, non-interactive auth, so this
adapter needs no OAuth/Workspace/quota workaround.

A generated `storage_ref` (uuid4 hex, no path separators, no filename) is
used as the object's flat key within the configured bucket. Mime type is
stored as the object's native Content-Type at upload time and read back on
download, exactly like DriveStorage does with Drive's mimeType metadata --
no sidecar file needed.

Unlike GoogleDriveClient/GoogleSheetsClient (sync SDKs wrapped in
asyncio.to_thread per docs/conventions.md -> Async), Supabase's Storage API
is called directly here via httpx.AsyncClient, which is natively async --
no thread-wrapping needed.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar
from uuid import uuid4

import httpx

from app.worker.retry import DEFAULT_DELAYS

T = TypeVar("T")


class SupabaseApiError(Exception):
    """Raised by a SupabaseStorageClient implementation (real or fake) for any
    Supabase Storage API failure, carrying an HTTP-like status code so
    SupabaseStorage can classify retryable vs. terminal failures. Message
    must never include the service_role key or a raw object URL."""

    def __init__(self, status: int, message: str = "Supabase Storage API error") -> None:
        self.status = status
        super().__init__(message)


class SupabaseStorageError(Exception):
    """Raised when a Supabase Storage operation fails terminally: retries
    exhausted, or a definite non-retryable error (404, 400, non-quota auth
    failure). Distinct from a "ref not found" case -- this means the
    operation itself could not be completed."""


def _is_retryable(exc: SupabaseApiError) -> bool:
    # 429 is Supabase/PostgREST's rate-limit response; 5xx is transient
    # server error. Everything else (400, 401, 403, 404) is terminal.
    return exc.status == 429 or exc.status >= 500


class SupabaseStorageClient(Protocol):
    """Async Supabase Storage API surface. The real implementation wraps
    httpx against Supabase's REST API; FakeSupabaseStorageClient is the only
    implementation any test may use."""

    async def upload(self, bucket: str, path: str, data: bytes, mime: str) -> None:
        """Uploads bytes to `bucket`/`path`."""
        ...

    async def download(self, bucket: str, path: str) -> tuple[bytes, str]:
        """Downloads `bucket`/`path`, returns (data, mime)."""
        ...

    async def exists(self, bucket: str, path: str) -> bool:
        """Cheap existence check for `bucket`/`path`."""
        ...


class HttpxSupabaseStorageClient:
    """Real SupabaseStorageClient backed by Supabase's Storage REST API."""

    def __init__(self, base_url: str, service_role_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {service_role_key}",
            "apikey": service_role_key,
        }

    async def upload(self, bucket: str, path: str, data: bytes, mime: str) -> None:
        url = f"{self._base_url}/storage/v1/object/{bucket}/{path}"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    url,
                    headers={**self._headers, "Content-Type": mime},
                    content=data,
                )
            except httpx.HTTPError as exc:
                raise SupabaseApiError(502, "Supabase upload request failed") from exc
        if resp.status_code >= 400:
            raise SupabaseApiError(resp.status_code, "Supabase upload request failed")

    async def download(self, bucket: str, path: str) -> tuple[bytes, str]:
        url = f"{self._base_url}/storage/v1/object/{bucket}/{path}"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=self._headers)
            except httpx.HTTPError as exc:
                raise SupabaseApiError(502, "Supabase download request failed") from exc
        if resp.status_code >= 400:
            raise SupabaseApiError(resp.status_code, "Supabase download request failed")
        mime = resp.headers.get("content-type", "application/octet-stream")
        return resp.content, mime

    async def exists(self, bucket: str, path: str) -> bool:
        url = f"{self._base_url}/storage/v1/object/info/{bucket}/{path}"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=self._headers)
            except httpx.HTTPError as exc:
                raise SupabaseApiError(502, "Supabase metadata request failed") from exc
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise SupabaseApiError(resp.status_code, "Supabase metadata request failed")
        return True


async def _retry_supabase_call(
    fn: Callable[[], Awaitable[T]],
    *,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
) -> T:
    """Retries `fn()` on retryable SupabaseApiError (429/5xx) using `delays`;
    raises SupabaseStorageError immediately on a terminal error, or once
    retries are exhausted. Mirrors app/storage/drive.py's `_retry_drive_call`.
    """
    last_exc: SupabaseApiError | None = None
    attempts = len(delays) + 1
    for attempt in range(attempts):
        try:
            return await fn()
        except SupabaseApiError as exc:
            if not _is_retryable(exc):
                raise SupabaseStorageError("Supabase Storage operation failed.") from exc
            last_exc = exc
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
    assert last_exc is not None
    raise SupabaseStorageError("Supabase Storage operation failed after retries.") from last_exc


class SupabaseStorage:
    """Supabase Storage-backed StorageAdapter. Uploads go into `bucket`; the
    generated uuid4 hex key is used directly as the opaque storage_ref."""

    def __init__(
        self,
        client: SupabaseStorageClient,
        bucket: str,
        *,
        retry_delays: tuple[float, ...] = DEFAULT_DELAYS,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._retry_delays = retry_delays

    async def put(self, data: bytes, filename: str, mime: str) -> str:
        ref = uuid4().hex

        async def _do() -> str:
            await self._client.upload(self._bucket, ref, data, mime)
            return ref

        return await _retry_supabase_call(_do, delays=self._retry_delays)

    async def get(self, ref: str) -> tuple[bytes, str]:
        async def _do() -> tuple[bytes, str]:
            return await self._client.download(self._bucket, ref)

        return await _retry_supabase_call(_do, delays=self._retry_delays)

    async def exists(self, ref: str) -> bool:
        async def _do() -> bool:
            return await self._client.exists(self._bucket, ref)

        return await _retry_supabase_call(_do, delays=self._retry_delays)

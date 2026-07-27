"""Google Drive-backed StorageAdapter (docs/conventions.md -> Adapters).

The Drive file ID returned by `put()` is used directly as the opaque
`storage_ref` (R17): it already has no path separators and no filename
embedded, so no additional wrapping is needed. Mime type is stored via
Drive's native `mimeType` metadata at upload time and read back at `get()`
time, rather than a sidecar file (LocalStorage's approach, not needed here).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from app.worker.retry import DEFAULT_DELAYS

T = TypeVar("T")

_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


class DriveApiError(Exception):
    """Raised by a DriveClient implementation (real or fake) for any Drive API
    failure, carrying an HTTP-like status code so DriveStorage can classify
    retryable vs. terminal failures. Message must never include a Drive URL,
    file path, or service-account identity."""

    def __init__(self, status: int, message: str = "Drive API error") -> None:
        self.status = status
        super().__init__(message)


class DriveStorageError(Exception):
    """Raised when a Drive operation fails terminally: retries exhausted, or a
    definite non-retryable error (e.g. 404, 400, non-quota permission denied).
    This is a distinct failure class from "ref not found" — it means the
    operation itself could not be completed. Message never leaks a Drive URL,
    path, or service-account identity."""


def _is_retryable(exc: DriveApiError) -> bool:
    # 403 covers Drive's rate-limit/quota responses; 5xx is transient server
    # error. Everything else (400, 404, non-quota 403 permission-denied is
    # indistinguishable from quota at this layer and is treated as retryable
    # by design — safer to retry a spurious 403 than to give up early) is
    # terminal.
    return exc.status == 403 or exc.status >= 500


class DriveClient(Protocol):
    """Synchronous Drive API surface. The real implementation wraps the
    Google SDK; FakeDriveClient is the only implementation any test may use."""

    def upload(self, folder_id: str, filename: str, data: bytes, mime: str) -> str:
        """Uploads bytes into `folder_id`, returns the new file's Drive ID."""
        ...

    def download(self, file_id: str) -> bytes:
        """Downloads and returns the raw bytes of `file_id`."""
        ...

    def get_metadata(self, file_id: str) -> dict[str, str]:
        """Cheap metadata fetch (no body download). At minimum returns
        {"id": ..., "mimeType": ...}. Raises DriveApiError(status=404, ...)
        if the file does not exist."""
        ...


class GoogleDriveClient:
    """Real DriveClient backed by the Google Drive v3 API. Synchronous by
    design — callers must wrap every call in asyncio.to_thread
    (docs/conventions.md -> Async)."""

    def __init__(self, service_account_info: dict[str, object]) -> None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            service_account_info,
            scopes=[_DRIVE_FILE_SCOPE],
        )
        self._service = build("drive", "v3", credentials=creds)

    def upload(self, folder_id: str, filename: str, data: bytes, mime: str) -> str:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaInMemoryUpload

        media = MediaInMemoryUpload(data, mimetype=mime, resumable=False)
        try:
            result = (
                self._service.files()
                .create(
                    body={"name": filename, "parents": [folder_id], "mimeType": mime},
                    media_body=media,
                    fields="id",
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveApiError(_http_error_status(exc), "Drive upload request failed") from exc
        file_id: str = result["id"]
        return file_id

    def download(self, file_id: str) -> bytes:
        import io

        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload

        try:
            request = self._service.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
            return buf.getvalue()
        except HttpError as exc:
            raise DriveApiError(_http_error_status(exc), "Drive download request failed") from exc

    def get_metadata(self, file_id: str) -> dict[str, str]:
        from googleapiclient.errors import HttpError

        try:
            result = (
                self._service.files().get(fileId=file_id, fields="id, mimeType").execute()
            )
        except HttpError as exc:
            raise DriveApiError(_http_error_status(exc), "Drive metadata request failed") from exc
        return {"id": result["id"], "mimeType": result.get("mimeType", "")}


def _http_error_status(exc: object) -> int:
    resp = getattr(exc, "resp", None)
    status: object = getattr(resp, "status", None)
    if isinstance(status, int | str):
        try:
            return int(status)
        except ValueError:
            return 500
    return 500


async def _retry_drive_call(
    fn: Callable[[], Awaitable[T]],
    *,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
) -> T:
    """Retries `fn()` on a retryable DriveApiError (403 quota/rate-limit, 5xx),
    up to `len(delays)` retries. Re-raises immediately (no retry) on a
    non-retryable DriveApiError. Re-raises the last DriveApiError if all
    attempts are exhausted. Never swallows or converts the exception type —
    callers decide how to map a DriveApiError to a public exception."""
    last_exc: DriveApiError | None = None
    attempts = len(delays) + 1
    for attempt in range(attempts):
        try:
            return await fn()
        except DriveApiError as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
    assert last_exc is not None
    raise last_exc


class DriveStorage:
    """Google Drive-backed StorageAdapter. Uploads go into `folder_id`; the
    returned Drive file ID is used directly as the opaque storage_ref."""

    def __init__(
        self,
        client: DriveClient,
        folder_id: str,
        *,
        retry_delays: tuple[float, ...] = DEFAULT_DELAYS,
    ) -> None:
        self._client = client
        self._folder_id = folder_id
        self._retry_delays = retry_delays

    async def put(self, data: bytes, filename: str, mime: str) -> str:
        async def _do() -> str:
            return await asyncio.to_thread(
                self._client.upload, self._folder_id, filename, data, mime
            )

        try:
            return await _retry_drive_call(_do, delays=self._retry_delays)
        except DriveApiError as exc:
            raise DriveStorageError("Drive upload failed.") from exc

    async def get(self, ref: str) -> tuple[bytes, str]:
        async def _download() -> bytes:
            return await asyncio.to_thread(self._client.download, ref)

        async def _meta() -> dict[str, str]:
            return await asyncio.to_thread(self._client.get_metadata, ref)

        try:
            data = await _retry_drive_call(_download, delays=self._retry_delays)
            meta = await _retry_drive_call(_meta, delays=self._retry_delays)
        except DriveApiError as exc:
            raise DriveStorageError("Drive download failed.") from exc
        return data, meta.get("mimeType", "application/octet-stream")

    async def exists(self, ref: str) -> bool:
        async def _do() -> dict[str, str]:
            return await asyncio.to_thread(self._client.get_metadata, ref)

        try:
            await _retry_drive_call(_do, delays=self._retry_delays)
            return True
        except DriveApiError as exc:
            if exc.status == 404:
                return False
            raise DriveStorageError("Drive existence check failed.") from exc

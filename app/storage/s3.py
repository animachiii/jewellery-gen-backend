"""S3-backed StorageAdapter.

Mirrors app/storage/supabase.py's shape exactly: a narrow async Protocol
(S3Client) with one real implementation (Boto3S3Client) and a fake used by
tests, so S3Storage itself is testable with no AWS surface at all.

boto3 is synchronous. Every call is wrapped in asyncio.to_thread rather than
introducing an async S3 library, matching app/storage/local.py, which does
the same for blocking filesystem I/O.

Credentials come from boto3's default chain — environment variables locally,
the EC2 instance profile in production. Never passed explicitly.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar
from uuid import uuid4

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.worker.retry import DEFAULT_DELAYS

T = TypeVar("T")


class S3ApiError(Exception):
    """Raised by an S3Client implementation (real or fake) for any S3 API
    failure, carrying the HTTP status so S3Storage can classify retryable
    vs. terminal failures. Message must never include a credential or a raw
    object URL — mirrors SupabaseApiError."""

    def __init__(self, status: int, message: str = "S3 API error") -> None:
        self.status = status
        super().__init__(message)


class S3StorageError(Exception):
    """Raised when an S3 operation fails terminally: retries exhausted, or a
    definite non-retryable error (404, 400, non-quota auth failure)."""


def _is_retryable(exc: S3ApiError) -> bool:
    # 503 SlowDown is S3's throttle response; 5xx is transient server error.
    # Everything else (400, 403, 404) is terminal.
    return exc.status == 429 or exc.status >= 500


class S3Client(Protocol):
    """Async S3 surface. The real implementation wraps boto3;
    FakeS3Client in tests is the only other implementation."""

    async def upload(self, bucket: str, key: str, data: bytes, mime: str) -> None: ...

    async def download(self, bucket: str, key: str) -> tuple[bytes, str]: ...

    async def exists(self, bucket: str, key: str) -> bool: ...


class Boto3S3Client:
    def __init__(self, region: str, endpoint_url: str | None = None) -> None:
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            # Retries are owned by _retry_s3_call below, the single tested
            # policy. Two nested retry layers would multiply attempts.
            config=Config(
                signature_version="s3v4",
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )

    @staticmethod
    def _status(exc: ClientError) -> int:
        return int(exc.response["ResponseMetadata"]["HTTPStatusCode"])

    async def upload(self, bucket: str, key: str, data: bytes, mime: str) -> None:
        def _put() -> None:
            try:
                self._client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=mime)
            except ClientError as exc:
                raise S3ApiError(self._status(exc)) from exc

        await asyncio.to_thread(_put)

    async def download(self, bucket: str, key: str) -> tuple[bytes, str]:
        def _get() -> tuple[bytes, str]:
            try:
                response = self._client.get_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                raise S3ApiError(self._status(exc)) from exc
            mime = str(response.get("ContentType", "application/octet-stream"))
            return bytes(response["Body"].read()), mime

        return await asyncio.to_thread(_get)

    async def exists(self, bucket: str, key: str) -> bool:
        def _head() -> bool:
            try:
                self._client.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if self._status(exc) == 404:
                    return False
                raise S3ApiError(self._status(exc)) from exc
            return True

        return await asyncio.to_thread(_head)


async def _retry_s3_call(
    call: Callable[[], Awaitable[T]], *, delays: tuple[float, ...] = DEFAULT_DELAYS
) -> T:
    last_exc: S3ApiError | None = None
    for attempt in range(len(delays) + 1):
        try:
            return await call()
        except S3ApiError as exc:
            if not _is_retryable(exc):
                raise S3StorageError("S3 operation failed.") from exc
            last_exc = exc
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
    assert last_exc is not None
    raise S3StorageError("S3 operation failed after retries.") from last_exc


class S3Storage:
    """S3-backed StorageAdapter. Objects go into `bucket`; the generated
    uuid4 hex key is used directly as the opaque storage_ref — same scheme
    SupabaseStorage uses, so refs are interchangeable in shape and nothing
    outside app/storage/ can tell the backends apart."""

    def __init__(
        self,
        client: S3Client,
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

        return await _retry_s3_call(_do, delays=self._retry_delays)

    async def get(self, ref: str) -> tuple[bytes, str]:
        async def _do() -> tuple[bytes, str]:
            return await self._client.download(self._bucket, ref)

        return await _retry_s3_call(_do, delays=self._retry_delays)

    async def exists(self, ref: str) -> bool:
        async def _do() -> bool:
            return await self._client.exists(self._bucket, ref)

        return await _retry_s3_call(_do, delays=self._retry_delays)

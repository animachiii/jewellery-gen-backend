"""app/storage/s3.py — the S3 StorageAdapter and its client.

Boto3S3Client is exercised against moto (real botocore serialization and
signing); S3Storage is exercised against FakeS3Client, mirroring how
tests/test_storage_supabase.py separates the two concerns.
"""

import boto3
import pytest
from moto import mock_aws

from app.storage.s3 import Boto3S3Client, S3ApiError, S3Storage, S3StorageError

BUCKET = "v1-test-bucket"


@pytest.fixture
def s3_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.mark.asyncio
async def test_upload_then_download_round_trips_data_and_mime(s3_bucket) -> None:
    client = Boto3S3Client(region="us-east-1")

    await client.upload(BUCKET, "ref123", b"image-bytes", "image/jpeg")
    data, mime = await client.download(BUCKET, "ref123")

    assert data == b"image-bytes"
    assert mime == "image/jpeg"


@pytest.mark.asyncio
async def test_exists_reflects_presence(s3_bucket) -> None:
    client = Boto3S3Client(region="us-east-1")

    assert await client.exists(BUCKET, "ref123") is False
    await client.upload(BUCKET, "ref123", b"x", "image/jpeg")
    assert await client.exists(BUCKET, "ref123") is True


@pytest.mark.asyncio
async def test_download_of_a_missing_key_raises_s3_api_error_404(s3_bucket) -> None:
    client = Boto3S3Client(region="us-east-1")

    with pytest.raises(S3ApiError) as excinfo:
        await client.download(BUCKET, "nope")

    assert excinfo.value.status == 404


@pytest.mark.asyncio
async def test_error_messages_never_leak_a_url_or_credential(s3_bucket) -> None:
    """Same rule SupabaseApiError carries: the message must not include a
    raw object URL or any credential."""
    client = Boto3S3Client(region="us-east-1")

    with pytest.raises(S3ApiError) as excinfo:
        await client.download(BUCKET, "nope")

    assert "http" not in str(excinfo.value).lower()
    assert BUCKET not in str(excinfo.value)


class FakeS3Client:
    """The only S3Client implementation besides Boto3S3Client. Mirrors
    tests/test_storage_supabase.py's FakeSupabaseStorageClient."""

    def __init__(self, fail_times: int = 0, status: int = 500) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.upload_calls = 0
        self._fail_times = fail_times
        self._status = status

    async def upload(self, bucket: str, key: str, data: bytes, mime: str) -> None:
        self.upload_calls += 1
        if self.upload_calls <= self._fail_times:
            raise S3ApiError(self._status)
        self.objects[key] = (data, mime)

    async def download(self, bucket: str, key: str) -> tuple[bytes, str]:
        if key not in self.objects:
            raise S3ApiError(404)
        return self.objects[key]

    async def exists(self, bucket: str, key: str) -> bool:
        return key in self.objects


@pytest.mark.asyncio
async def test_put_returns_an_opaque_ref_with_no_path_or_filename() -> None:
    storage = S3Storage(FakeS3Client(), BUCKET)

    ref = await storage.put(b"bytes", "client-supplied-name.jpg", "image/jpeg")

    assert "/" not in ref
    assert "client-supplied-name" not in ref
    assert len(ref) == 32


@pytest.mark.asyncio
async def test_put_then_get_round_trips() -> None:
    storage = S3Storage(FakeS3Client(), BUCKET)

    ref = await storage.put(b"bytes", "x.jpg", "image/jpeg")

    assert await storage.get(ref) == (b"bytes", "image/jpeg")


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried() -> None:
    client = FakeS3Client(fail_times=1, status=500)
    storage = S3Storage(client, BUCKET, retry_delays=(0.0,))

    ref = await storage.put(b"bytes", "x.jpg", "image/jpeg")

    assert client.upload_calls == 2
    assert await storage.exists(ref) is True


@pytest.mark.asyncio
async def test_a_terminal_failure_is_not_retried() -> None:
    client = FakeS3Client(fail_times=1, status=403)
    storage = S3Storage(client, BUCKET, retry_delays=(0.0,))

    with pytest.raises(S3StorageError):
        await storage.put(b"bytes", "x.jpg", "image/jpeg")

    assert client.upload_calls == 1, "a 403 must not be retried"


def test_factory_resolves_s3_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings
    from app.storage.factory import get_storage_adapter

    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "aws_region", "us-east-1")

    adapter = get_storage_adapter()

    assert isinstance(adapter, S3Storage)

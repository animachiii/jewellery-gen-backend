"""app/storage/s3.py — the S3 StorageAdapter and its client.

Boto3S3Client is exercised against moto (real botocore serialization and
signing); S3Storage is exercised against FakeS3Client, mirroring how
tests/test_storage_supabase.py separates the two concerns.
"""

import boto3
import pytest
from moto import mock_aws

from app.storage.s3 import Boto3S3Client, S3ApiError

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

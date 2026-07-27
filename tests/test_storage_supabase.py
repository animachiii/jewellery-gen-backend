import pytest

from app.storage.base import StorageAdapter
from app.storage.supabase import SupabaseStorage, SupabaseStorageError
from tests.fakes.fake_supabase_client import FakeSupabaseStorageClient

FAST_DELAYS = (0.0, 0.0, 0.0)


@pytest.fixture
def fake_client() -> FakeSupabaseStorageClient:
    return FakeSupabaseStorageClient()


@pytest.fixture
def storage(fake_client: FakeSupabaseStorageClient) -> SupabaseStorage:
    return SupabaseStorage(fake_client, bucket="test-bucket", retry_delays=FAST_DELAYS)


async def test_put_get_round_trips_bytes_and_mime(storage: SupabaseStorage) -> None:
    ref = await storage.put(b"hello world", "photo.jpg", "image/jpeg")
    data, mime = await storage.get(ref)
    assert data == b"hello world"
    assert mime == "image/jpeg"


async def test_ref_has_no_path_separators_or_filename(storage: SupabaseStorage) -> None:
    ref = await storage.put(b"data", "secret-filename.png", "image/png")
    assert "/" not in ref
    assert "\\" not in ref
    assert "secret-filename" not in ref
    assert ".png" not in ref


async def test_exists_true_after_upload(
    storage: SupabaseStorage, fake_client: FakeSupabaseStorageClient
) -> None:
    ref = await storage.put(b"x", "f.png", "image/png")
    fake_client.calls.clear()

    result = await storage.exists(ref)

    assert result is True
    assert [c.method for c in fake_client.calls] == ["exists"]


async def test_exists_false_for_unknown_ref(
    storage: SupabaseStorage, fake_client: FakeSupabaseStorageClient
) -> None:
    result = await storage.exists("nonexistent-ref")

    assert result is False
    assert [c.method for c in fake_client.calls] == ["exists"]


async def test_rate_limit_failure_retries_then_raises(
    storage: SupabaseStorage, fake_client: FakeSupabaseStorageClient
) -> None:
    ref = await storage.put(b"x", "f.png", "image/png")
    fake_client.calls.clear()
    fake_client.fail_mode = "rate_limit"
    fake_client.fail_remaining = -1  # always fails

    with pytest.raises(SupabaseStorageError):
        await storage.get(ref)

    # 1 initial attempt + len(FAST_DELAYS) retries for the download call.
    download_calls = [c for c in fake_client.calls if c.method == "download"]
    assert len(download_calls) == 4


async def test_server_error_retries_then_raises(
    storage: SupabaseStorage, fake_client: FakeSupabaseStorageClient
) -> None:
    ref = await storage.put(b"x", "f.png", "image/png")
    fake_client.calls.clear()
    fake_client.fail_mode = "server_error"
    fake_client.fail_remaining = -1

    with pytest.raises(SupabaseStorageError):
        await storage.exists(ref)

    assert len(fake_client.calls) == 4


async def test_definite_not_found_does_not_retry(
    storage: SupabaseStorage, fake_client: FakeSupabaseStorageClient
) -> None:
    fake_client.fail_mode = "not_found"
    fake_client.fail_remaining = -1

    with pytest.raises(SupabaseStorageError):
        await storage.get("some-ref")

    download_calls = [c for c in fake_client.calls if c.method == "download"]
    assert len(download_calls) == 1


async def test_supabase_storage_satisfies_storage_adapter_protocol(
    fake_client: FakeSupabaseStorageClient,
) -> None:
    adapter: StorageAdapter = SupabaseStorage(fake_client, bucket="test-bucket")
    assert adapter is not None


async def test_source_image_and_generated_asset_round_trip_identically(
    storage: SupabaseStorage,
) -> None:
    """Same guarantee as DriveStorage/LocalStorage: one adapter, one set of
    guarantees, for both the incoming source image and generated assets."""
    job_id = "job-retention-1"

    source_ref = await storage.put(b"source-bytes", filename=f"{job_id}.png", mime="image/png")
    asset_ref = await storage.put(b"asset-bytes", filename=f"{job_id}_0", mime="image/png")

    source_data, source_mime = await storage.get(source_ref)
    asset_data, asset_mime = await storage.get(asset_ref)

    assert (source_data, source_mime) == (b"source-bytes", "image/png")
    assert (asset_data, asset_mime) == (b"asset-bytes", "image/png")
    assert source_ref != asset_ref
    for ref in (source_ref, asset_ref):
        assert "/" not in ref and "\\" not in ref

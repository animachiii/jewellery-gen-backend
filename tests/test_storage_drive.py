import pytest

from app.storage.base import StorageAdapter
from app.storage.drive import DriveStorage, DriveStorageError
from tests.fakes.fake_drive_client import FakeDriveClient

FAST_DELAYS = (0.0, 0.0, 0.0)


@pytest.fixture
def fake_client() -> FakeDriveClient:
    return FakeDriveClient()


@pytest.fixture
def storage(fake_client: FakeDriveClient) -> DriveStorage:
    return DriveStorage(fake_client, folder_id="folder-123", retry_delays=FAST_DELAYS)


async def test_put_get_round_trips_bytes_and_mime(storage: DriveStorage) -> None:
    ref = await storage.put(b"hello world", "photo.jpg", "image/jpeg")
    data, mime = await storage.get(ref)
    assert data == b"hello world"
    assert mime == "image/jpeg"


async def test_ref_has_no_path_separators_or_filename(storage: DriveStorage) -> None:
    ref = await storage.put(b"data", "secret-filename.png", "image/png")
    assert "/" not in ref
    assert "\\" not in ref
    assert "secret-filename" not in ref
    assert ".png" not in ref


async def test_exists_true_without_full_download(
    storage: DriveStorage, fake_client: FakeDriveClient
) -> None:
    ref = await storage.put(b"x", "f.png", "image/png")
    fake_client.calls.clear()

    result = await storage.exists(ref)

    assert result is True
    assert [c.method for c in fake_client.calls] == ["get_metadata"]


async def test_exists_false_for_unknown_ref_without_full_download(
    storage: DriveStorage, fake_client: FakeDriveClient
) -> None:
    result = await storage.exists("nonexistent-id")

    assert result is False
    assert [c.method for c in fake_client.calls] == ["get_metadata"]


async def test_quota_failure_retries_then_raises(
    storage: DriveStorage, fake_client: FakeDriveClient
) -> None:
    ref = await storage.put(b"x", "f.png", "image/png")
    fake_client.calls.clear()
    fake_client.fail_mode = "quota"
    fake_client.fail_remaining = -1  # always fails

    with pytest.raises(DriveStorageError):
        await storage.get(ref)

    # 1 initial attempt + len(DEFAULT_DELAYS) retries for the download call.
    download_calls = [c for c in fake_client.calls if c.method == "download"]
    assert len(download_calls) == 4


async def test_server_error_retries_then_raises(
    storage: DriveStorage, fake_client: FakeDriveClient
) -> None:
    ref = await storage.put(b"x", "f.png", "image/png")
    fake_client.calls.clear()
    fake_client.fail_mode = "server_error"
    fake_client.fail_remaining = -1

    with pytest.raises(DriveStorageError):
        await storage.exists(ref)

    assert len(fake_client.calls) == 4


async def test_definite_not_found_does_not_retry(
    storage: DriveStorage, fake_client: FakeDriveClient
) -> None:
    fake_client.fail_mode = "not_found"
    fake_client.fail_remaining = -1

    with pytest.raises(DriveStorageError):
        await storage.get("some-id")

    download_calls = [c for c in fake_client.calls if c.method == "download"]
    assert len(download_calls) == 1


async def test_drive_storage_satisfies_storage_adapter_protocol(
    fake_client: FakeDriveClient,
) -> None:
    adapter: StorageAdapter = DriveStorage(fake_client, folder_id="folder-123")
    assert adapter is not None


async def test_source_image_and_generated_asset_round_trip_identically(
    storage: DriveStorage,
) -> None:
    """Phase 2 Step 2, Checkpoint 2: a source image (filename convention used
    by app/api/v1/generate.py: "{job_id}.{ext}") and a generated asset
    (filename convention used by app/worker/tasks.py: "{job_id}_{i}") go
    through the same DriveStorage adapter with the same guarantees — one
    adapter, one set of guarantees, for both source and output."""
    job_id = "job-retention-1"

    source_ref = await storage.put(b"source-bytes", filename=f"{job_id}.png", mime="image/png")
    asset_ref = await storage.put(b"asset-bytes", filename=f"{job_id}_0", mime="image/png")

    source_data, source_mime = await storage.get(source_ref)
    asset_data, asset_mime = await storage.get(asset_ref)

    assert (source_data, source_mime) == (b"source-bytes", "image/png")
    assert (asset_data, asset_mime) == (b"asset-bytes", "image/png")
    # Refs are opaque and distinguishable from each other, but neither embeds
    # a path separator or filename (R17 — same assertion style as Checkpoint 1).
    assert source_ref != asset_ref
    for ref in (source_ref, asset_ref):
        assert "/" not in ref and "\\" not in ref

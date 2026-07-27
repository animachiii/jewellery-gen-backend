import os

import pytest

from app.config import settings
from app.storage.base import StorageAdapter
from app.storage.drive import DriveStorage
from app.storage.factory import get_storage_adapter
from app.storage.local import LocalStorage, StorageRefNotFoundError
from tests.fakes.fake_drive_client import FakeDriveClient


@pytest.fixture
def storage(tmp_path: object) -> LocalStorage:
    return LocalStorage(base_dir=str(tmp_path))


async def test_put_get_round_trips_bytes_and_mime(storage: LocalStorage) -> None:
    ref = await storage.put(b"hello world", "photo.jpg", "image/jpeg")
    data, mime = await storage.get(ref)
    assert data == b"hello world"
    assert mime == "image/jpeg"


async def test_ref_has_no_path_separators_or_filename(storage: LocalStorage) -> None:
    ref = await storage.put(b"data", "secret-filename.png", "image/png")
    assert "/" not in ref
    assert "\\" not in ref
    assert os.sep not in ref
    assert "secret-filename" not in ref
    assert ".png" not in ref


async def test_exists_true_for_stored_ref(storage: LocalStorage) -> None:
    ref = await storage.put(b"x", "f.png", "image/png")
    assert await storage.exists(ref) is True


async def test_exists_false_for_unknown_ref(storage: LocalStorage) -> None:
    assert await storage.exists("nonexistent") is False


async def test_get_unknown_ref_raises_without_leaking_path(storage: LocalStorage) -> None:
    with pytest.raises(StorageRefNotFoundError) as exc_info:
        await storage.get("nonexistent")
    assert str(storage._base_dir) not in str(exc_info.value)


async def test_get_storage_adapter_returns_local_storage() -> None:
    adapter = get_storage_adapter()
    assert isinstance(adapter, LocalStorage)


def test_local_storage_satisfies_storage_adapter_protocol() -> None:
    adapter: StorageAdapter = LocalStorage()
    assert adapter is not None


def test_get_storage_adapter_returns_drive_storage_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_backend", "drive")
    monkeypatch.setattr("app.storage.drive.GoogleDriveClient", lambda info: FakeDriveClient())

    adapter = get_storage_adapter()

    assert isinstance(adapter, DriveStorage)

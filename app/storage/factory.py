from app.config import settings
from app.storage.base import StorageAdapter
from app.storage.local import LocalStorage


def get_storage_adapter() -> StorageAdapter:
    """Resolve the configured StorageAdapter.

    Defaults to LocalStorage (`STORAGE_BACKEND=local`) so Phase 1's behaviour
    and test suite remain regression-free by default. `STORAGE_BACKEND=drive`
    resolves to DriveStorage instead — no other code change required.
    """
    if settings.storage_backend == "drive":
        from app.storage.drive import DriveStorage, GoogleDriveClient

        client = GoogleDriveClient(settings.google_service_account_info)
        return DriveStorage(client, settings.gdrive_folder_id)
    return LocalStorage()

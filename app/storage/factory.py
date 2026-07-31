from app.config import settings
from app.storage.base import StorageAdapter
from app.storage.local import LocalStorage


def get_storage_adapter() -> StorageAdapter:
    """Resolve the configured StorageAdapter.

    Defaults to LocalStorage (`STORAGE_BACKEND=local`) so Phase 1's behaviour
    and test suite remain regression-free by default. `STORAGE_BACKEND=drive`
    resolves to DriveStorage; `STORAGE_BACKEND=supabase` resolves to
    SupabaseStorage — no other code change required either way.
    """
    if settings.storage_backend == "drive":
        from app.storage.drive import DriveStorage, GoogleDriveClient

        assert settings.gdrive_folder_id is not None
        drive_client = GoogleDriveClient(settings.google_service_account_info)
        return DriveStorage(drive_client, settings.gdrive_folder_id)
    if settings.storage_backend == "supabase":
        from app.storage.supabase import HttpxSupabaseStorageClient, SupabaseStorage

        assert settings.supabase_url is not None
        assert settings.supabase_service_role_key is not None
        assert settings.supabase_storage_bucket is not None
        supabase_client = HttpxSupabaseStorageClient(
            settings.supabase_url, settings.supabase_service_role_key
        )
        return SupabaseStorage(supabase_client, settings.supabase_storage_bucket)
    return LocalStorage()

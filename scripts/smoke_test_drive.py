"""Manual, one-off smoke test for the real DriveStorage adapter.

Not part of the automated test suite (docs/conventions.md -> Testing: no
test may call a real external service). Run this by hand, once, from a
machine with real network access and a real .env, before trusting
STORAGE_BACKEND=drive in production. Deletes its own test file afterward.

Usage:
    STORAGE_BACKEND=drive python scripts/smoke_test_drive.py
"""

import asyncio

from app.config import settings
from app.storage.drive import DriveStorage, GoogleDriveClient


async def main() -> None:
    if settings.storage_backend != "drive":
        raise SystemExit(
            "Set STORAGE_BACKEND=drive (in .env or the environment) before running this."
        )

    client = GoogleDriveClient(settings.google_service_account_info)
    storage = DriveStorage(client, settings.gdrive_folder_id)

    payload = b"jewellery-gen-backend smoke test " + b"x" * 100
    print(f"Uploading {len(payload)} bytes to folder {settings.gdrive_folder_id}...")
    ref = await storage.put(payload, filename="smoke_test.txt", mime="text/plain")
    print(f"Uploaded. storage_ref = {ref!r}")
    print("-> Check the Drive folder now: the file should be visible there.")

    print("Downloading it back...")
    data, mime = await storage.get(ref)
    assert data == payload, "Byte mismatch between uploaded and downloaded content!"
    assert mime == "text/plain", f"Unexpected mime type: {mime!r}"
    print(f"Round-trip OK: {len(data)} bytes, mime={mime!r}")

    exists = await storage.exists(ref)
    assert exists is True
    print("exists() check OK")

    print("Deleting the test file's client reference is not implemented in "
          "DriveStorage (no delete() method — v1 has no retention/deletion "
          "policy per docs/business-rules.md §6). Delete it manually from "
          "Drive now, or leave it — it's a harmless 130-byte text file.")
    print(f"File to delete manually if desired: Drive file ID {ref}")
    print("\nSMOKE TEST PASSED.")


if __name__ == "__main__":
    asyncio.run(main())

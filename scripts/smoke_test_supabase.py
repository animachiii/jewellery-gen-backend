"""Manual, one-off smoke test for the real SupabaseStorage adapter.

Not part of the automated test suite (docs/conventions.md -> Testing: no
test may call a real external service). Run this by hand, once, from a
machine with real network access and a real .env, before trusting
STORAGE_BACKEND=supabase in production.

Usage:
    STORAGE_BACKEND=supabase python scripts/smoke_test_supabase.py
"""

import asyncio

from app.config import settings
from app.storage.supabase import HttpxSupabaseStorageClient, SupabaseStorage


async def main() -> None:
    if settings.storage_backend != "supabase":
        raise SystemExit(
            "Set STORAGE_BACKEND=supabase (in .env or the environment) before running this."
        )
    assert settings.supabase_url is not None
    assert settings.supabase_service_role_key is not None
    assert settings.supabase_storage_bucket is not None

    client = HttpxSupabaseStorageClient(settings.supabase_url, settings.supabase_service_role_key)
    storage = SupabaseStorage(client, settings.supabase_storage_bucket)

    payload = b"jewellery-gen-backend smoke test " + b"x" * 100
    print(f"Uploading {len(payload)} bytes to bucket {settings.supabase_storage_bucket!r}...")
    ref = await storage.put(payload, filename="smoke_test.txt", mime="text/plain")
    print(f"Uploaded. storage_ref = {ref!r}")
    print("-> Check the bucket now in the Supabase dashboard: the object should be visible there.")

    print("Downloading it back...")
    data, mime = await storage.get(ref)
    assert data == payload, "Byte mismatch between uploaded and downloaded content!"
    assert mime == "text/plain", f"Unexpected mime type: {mime!r}"
    print(f"Round-trip OK: {len(data)} bytes, mime={mime!r}")

    exists = await storage.exists(ref)
    assert exists is True
    print("exists() check OK")

    print(
        "SupabaseStorage has no delete() method (no retention/deletion policy "
        "in v1, per docs/business-rules.md §6). Delete the test object manually "
        "from the Supabase dashboard if desired, or leave it (harmless)."
    )
    print(f"Object to delete manually if desired: {ref}")
    print("\nSMOKE TEST PASSED.")


if __name__ == "__main__":
    asyncio.run(main())

from dataclasses import dataclass, field
from typing import Literal

from app.storage.supabase import SupabaseApiError

FailMode = Literal["none", "rate_limit", "server_error", "not_found"]

_STATUS_BY_MODE: dict[FailMode, int] = {
    "rate_limit": 429,
    "server_error": 500,
    "not_found": 404,
}


@dataclass
class RecordedSupabaseCall:
    method: str
    path: str | None = None


@dataclass
class FakeSupabaseStorageClient:
    """In-memory SupabaseStorageClient standing in for the real Supabase
    Storage REST API (docs/conventions.md -> Testing: no test may call a
    real external service).

    `fail_mode` + `fail_remaining` simulate a failure on every subsequent
    call until exhausted: set `fail_remaining=-1` for "always fails" (to
    prove retry-exhaustion), or a positive count for "fails N times then
    succeeds".
    """

    calls: list[RecordedSupabaseCall] = field(default_factory=list)
    fail_mode: FailMode = "none"
    fail_remaining: int = 0

    _objects: dict[str, tuple[bytes, str]] = field(default_factory=dict)

    def _maybe_fail(self) -> None:
        if self.fail_mode == "none" or self.fail_remaining == 0:
            return
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
        status = _STATUS_BY_MODE[self.fail_mode]
        raise SupabaseApiError(status, f"simulated Supabase {self.fail_mode} failure")

    async def upload(self, bucket: str, path: str, data: bytes, mime: str) -> None:
        self.calls.append(RecordedSupabaseCall("upload", path))
        self._maybe_fail()
        self._objects[path] = (data, mime)

    async def download(self, bucket: str, path: str) -> tuple[bytes, str]:
        self.calls.append(RecordedSupabaseCall("download", path))
        self._maybe_fail()
        if path not in self._objects:
            raise SupabaseApiError(404, "simulated Supabase object-not-found")
        return self._objects[path]

    async def exists(self, bucket: str, path: str) -> bool:
        self.calls.append(RecordedSupabaseCall("exists", path))
        self._maybe_fail()
        return path in self._objects

from dataclasses import dataclass, field
from typing import Literal

from app.storage.drive import DriveApiError

FailMode = Literal["none", "quota", "server_error", "not_found"]

_STATUS_BY_MODE: dict[FailMode, int] = {
    "quota": 403,
    "server_error": 500,
    "not_found": 404,
}


@dataclass
class RecordedDriveCall:
    method: str
    file_id: str | None = None


@dataclass
class FakeDriveClient:
    """In-memory DriveClient standing in for the real Drive API
    (docs/conventions.md -> Testing: no test may call a real external service).

    `fail_mode` + `fail_remaining` simulate a failure on every subsequent call
    until exhausted: set `fail_remaining=-1` for "always fails" (to prove
    retry-exhaustion), or a positive count for "fails N times then succeeds".
    """

    calls: list[RecordedDriveCall] = field(default_factory=list)
    fail_mode: FailMode = "none"
    fail_remaining: int = 0

    _files: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    _next_id: int = 1

    def _maybe_fail(self) -> None:
        if self.fail_mode == "none" or self.fail_remaining == 0:
            return
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
        status = _STATUS_BY_MODE[self.fail_mode]
        raise DriveApiError(status, f"simulated Drive {self.fail_mode} failure")

    def upload(self, folder_id: str, filename: str, data: bytes, mime: str) -> str:
        self.calls.append(RecordedDriveCall("upload"))
        self._maybe_fail()
        file_id = f"fake-drive-file-{self._next_id}"
        self._next_id += 1
        self._files[file_id] = (data, mime)
        return file_id

    def download(self, file_id: str) -> bytes:
        self.calls.append(RecordedDriveCall("download", file_id))
        self._maybe_fail()
        if file_id not in self._files:
            raise DriveApiError(404, "simulated Drive file-not-found")
        return self._files[file_id][0]

    def get_metadata(self, file_id: str) -> dict[str, str]:
        self.calls.append(RecordedDriveCall("get_metadata", file_id))
        self._maybe_fail()
        if file_id not in self._files:
            raise DriveApiError(404, "simulated Drive file-not-found")
        data, mime = self._files[file_id]
        return {"id": file_id, "mimeType": mime}

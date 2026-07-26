import asyncio
import re
from typing import Protocol

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.models.job import Job

log = get_logger(__name__)

LOCK_KEY = "lock:sheets:write"
LOCK_TTL_SECONDS = 15
LOCK_RETRY_DELAY_SECONDS = 0.2
LOCK_MAX_ATTEMPTS = 100  # ~20s worst case, comfortably above the 15s lock TTL

_UPDATED_RANGE_ROW_RE = re.compile(r"![A-Z]+(\d+):")

_sheets_write_failures = 0


def sheets_write_failures() -> int:
    """Exposed for Phase 8 metrics."""
    return _sheets_write_failures


class SheetsClient(Protocol):
    """Synchronous Sheets API surface. The real implementation wraps the Google
    SDK; FakeSheetsClient is the only implementation any test may use."""

    def append_row(self, sheet_id: str, tab: str, values: list[str]) -> str:
        """Returns the API's updatedRange, e.g. 'JobLog!A47:G47'."""
        ...

    def update_row(self, sheet_id: str, range_: str, values: list[str]) -> None: ...

    def read_all_rows(self, sheet_id: str, tab: str) -> list[list[str]]:
        """Row 1 is the header; each subsequent row is A..T, ragged (Sheets omits
        trailing blank cells)."""
        ...


def _job_create_row(job: Job) -> list[str]:
    return [
        job.job_id,
        job.created_at.isoformat().replace("+00:00", "Z"),
        job.api_key_name,
        job.service.value,
        job.jewelry_type_requested.value if job.jewelry_type_requested else "AUTO",
        "1" if job.mock else "0",
        job.content_hash,
    ]


def _job_terminal_row(job: Job) -> list[str]:
    duration_seconds = ""
    if job.completed_at is not None:
        duration_seconds = str(int((job.completed_at - job.created_at).total_seconds()))
    return [
        job.status.value,
        job.completed_at.isoformat().replace("+00:00", "Z") if job.completed_at else "",
        duration_seconds,
        job.jewelry_type_final.value if job.jewelry_type_final else "",
        job.type_source.value if job.type_source else "",
        str(job.confidence) if job.confidence is not None else "",
        job.provider or "",
        job.provider_job_id or "",
        str(len(job.asset_refs)),
        ",".join(job.asset_refs),
        job.error_code.value if job.error_code else "",
        job.error_message or "",
        job.prompt_snapshot or "",
    ]


def _parse_row_index(updated_range: str) -> int:
    match = _UPDATED_RANGE_ROW_RE.search(updated_range)
    if not match:
        raise ValueError(f"Could not parse row index from updatedRange {updated_range!r}")
    return int(match.group(1))


async def _with_lock(redis: Redis, fn: object) -> object:
    for _ in range(LOCK_MAX_ATTEMPTS):
        acquired = await redis.set(LOCK_KEY, "1", nx=True, ex=LOCK_TTL_SECONDS)
        if acquired:
            try:
                return await fn()  # type: ignore[operator]
            finally:
                await redis.delete(LOCK_KEY)
        await asyncio.sleep(LOCK_RETRY_DELAY_SECONDS)
    raise TimeoutError("Timed out waiting for lock:sheets:write")


async def append_job_row(
    redis: Redis, client: SheetsClient, sheet_id: str, tab: str, job: Job
) -> int:
    async def _do() -> int:
        values = _job_create_row(job)
        updated_range = await asyncio.to_thread(client.append_row, sheet_id, tab, values)
        return _parse_row_index(updated_range)

    result = await _with_lock(redis, _do)
    return result  # type: ignore[return-value]


async def update_job_row(
    redis: Redis, client: SheetsClient, sheet_id: str, tab: str, job: Job, row_index: int
) -> None:
    async def _do() -> None:
        values = _job_terminal_row(job)
        range_ = f"{tab}!H{row_index}:T{row_index}"
        await asyncio.to_thread(client.update_row, sheet_id, range_, values)

    await _with_lock(redis, _do)


async def safe_append_job_row(
    redis: Redis, client: SheetsClient, sheet_id: str, tab: str, job: Job
) -> int | None:
    global _sheets_write_failures
    try:
        return await append_job_row(redis, client, sheet_id, tab, job)
    except Exception:
        _sheets_write_failures += 1
        log.warning("sheets.write.failed", job_id=job.job_id, stage="append")
        return None


async def safe_update_job_row(
    redis: Redis, client: SheetsClient, sheet_id: str, tab: str, job: Job, row_index: int
) -> None:
    global _sheets_write_failures
    try:
        await update_job_row(redis, client, sheet_id, tab, job, row_index)
    except Exception:
        _sheets_write_failures += 1
        log.warning("sheets.write.failed", job_id=job.job_id, stage="update")


class GoogleSheetsClient:
    """Real SheetsClient backed by the Google Sheets API. Synchronous by design —
    callers must wrap every call in asyncio.to_thread (docs/conventions.md → Async)."""

    def __init__(self, service_account_info: dict[str, object]) -> None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            service_account_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self._service = build("sheets", "v4", credentials=creds)

    def append_row(self, sheet_id: str, tab: str, values: list[str]) -> str:
        result = (
            self._service.spreadsheets()
            .values()
            .append(
                spreadsheetId=sheet_id,
                range=f"{tab}!A:G",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [values]},
            )
            .execute()
        )
        updates: dict[str, str] = result["updates"]
        return updates["updatedRange"]

    def update_row(self, sheet_id: str, range_: str, values: list[str]) -> None:
        self._service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_,
            valueInputOption="RAW",
            body={"values": [values]},
        ).execute()

    def read_all_rows(self, sheet_id: str, tab: str) -> list[list[str]]:
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{tab}!A:T")
            .execute()
        )
        values: list[list[str]] = result.get("values", [])
        return values

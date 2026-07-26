from arq import cron
from arq.connections import RedisSettings
from redis.asyncio import Redis

from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.store.rehydrate import rehydrate
from app.store.sheets_store import GoogleSheetsClient, SheetsClient
from app.worker.sweeper import sweep
from app.worker.tasks import run_job_pipeline, run_job_pipeline_from_resolve

log = get_logger(__name__)

JOB_LOG_TAB = "JobLog"


def _build_sheets_client() -> SheetsClient | None:
    try:
        return GoogleSheetsClient(settings.google_service_account_info)
    except Exception:
        log.warning("sheets.client.unavailable")
        return None


async def on_startup(ctx: dict[str, object]) -> None:
    configure_logging()
    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    ctx["app_redis"] = redis
    sheets_client = _build_sheets_client()
    ctx["sheets_client"] = sheets_client
    log.info("worker.started")

    if sheets_client is not None:
        try:
            recreated = await rehydrate(redis, sheets_client, settings.google_sheet_id, JOB_LOG_TAB)
            log.info("worker.rehydrated", count=len(recreated))
        except Exception:
            log.warning("worker.rehydration.failed")


async def on_shutdown(ctx: dict[str, object]) -> None:
    redis = ctx.get("app_redis")
    if isinstance(redis, Redis):
        await redis.aclose()
    log.info("worker.stopped")


async def noop(ctx: dict[str, object]) -> None:
    """Placeholder task so arq has at least one registered function pre-Phase-0b."""


async def sweep_cron(ctx: dict[str, object]) -> None:
    redis = ctx["app_redis"]
    sheets_client = ctx.get("sheets_client")
    assert isinstance(redis, Redis)
    await sweep(redis, sheets_client, settings.google_sheet_id, JOB_LOG_TAB)  # type: ignore[arg-type]


class WorkerSettings:
    functions: list[object] = [noop, run_job_pipeline, run_job_pipeline_from_resolve]
    cron_jobs: list[object] = [cron(sweep_cron, second=0, run_at_startup=False)]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_concurrency
    on_startup = on_startup
    on_shutdown = on_shutdown
